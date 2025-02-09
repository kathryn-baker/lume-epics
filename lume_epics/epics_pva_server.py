import logging
import copy
import multiprocessing
from multiprocessing.managers import DictProxy
from queue import Full, Empty
from lume_epics import model
import numpy as np
import time
import signal
from typing import List, Union
from functools import partial
from typing import Dict
from lume_model.variables import InputVariable, OutputVariable
from p4p.client.thread import Context
from p4p.nt import NTScalar, NTNDArray, NTTable
from p4p.server.thread import SharedPV
from p4p.server import Server as P4PServer
from p4p.nt.ndarray import ntndarray as NTNDArrayData
from p4p.server.raw import ServOpWrap
from p4p import Value, Type

p4p_logger = logging.getLogger("p4p")
p4p_logger.setLevel("DEBUG")
# Each server must have their outQueue in which the comm server will set the inputs and outputs vars to be updated
# Comm server must also provide one inQueue in which it will receive inputs from Servers

logger = logging.getLogger(__name__)


class PVAServer(multiprocessing.Process):
    """
    Process-based implementation of Channel Access server.

    Attributes:
        pva_server (P4PServer): p4p server instance
        exit_event (multiprocessing.Event): Event indicating pvAccess server error and communicating to main
        shutdown_event (multiprocessing.Event): Event indicating shutdown
        _input_variables (List[InputVariable]): List of input variables
        _output_variables (List[OutputVariable]): List of output variables
        _in_queue (multiprocessing.Queue): input variable queue
        _out_queue (multiprocessing.Queue): output variable update queue
        _providers (dict): Dictionary mapping pvname to p4p provider
        _running_indicator (multiprocessing.Value): Boolean indicator of running model execution
        _monitors (dict): Dictionary of monitor objects for read-only server
        _cached_values (dict): Dict for caching values while model executes
        _pvname_to_varname_map (dict): Mapping of pvname to variable name
        _varname_to_pvname_map (dict): Mapping of variable name to pvame

    """

    protocol = "pva"

    def __init__(
        self,
        input_variables: List[InputVariable],
        output_variables: List[OutputVariable],
        epics_config: dict,
        in_queue: multiprocessing.Queue,
        out_queue: multiprocessing.Queue,
        running_indicator: multiprocessing.Value,
        *args,
        **kwargs,
    ) -> None:
        """Initialize server process.

        Args:
            input_variables (Dict[str, InputVariable]): Dictionary mapping pvname to lume-model input variable.

            output_variables (Dict[str, OutputVariable]):Dictionary mapping pvname to lume-model output variable.

            epics_config (dict): Dictionary describing EPICS configuration for model variables

            in_queue (multiprocessing.Queue): Queue for tracking updates to input variables

            out_queue (multiprocessing.Queue): Queue for tracking updates to output variables

            running_indicator (multiprocessing.Value): Boolean indicator indicating running model execution

        """

        super().__init__(*args, **kwargs)
        self.pva_server = None
        self.exit_event = multiprocessing.Event()
        self.shutdown_event = multiprocessing.Event()
        self._input_variables = input_variables
        self._output_variables = output_variables
        self._epics_config = epics_config
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._providers = {}
        self._running_indicator = running_indicator
        # monitors for read only
        self._monitors = {}
        self._cached_values = {}
        self._field_to_parent_map = {}

        # utility maps
        self._pvname_to_varname_map = {
            config["pvname"]: var_name for var_name, config in epics_config.items()
        }
        self._varname_to_pvname_map = {
            var_name: config["pvname"] for var_name, config in epics_config.items()
        }

    def update_pv(self, pvname: str, value: Union[np.ndarray, float]) -> None:
        """Adds update to input process variable to the input queue.

        Args:
            pvname (str): Name of process variable

            value (Union[np.ndarray, float]): Value to set

        """
        # Hack for now to get the pickable value
        value = value.raw.value

        varname = self._pvname_to_varname_map[pvname]
        model_variable = self._input_variables[varname]

        if model_variable.variable_type == "image":
            model_variable.x_min = value.attrib["x_min"]
            model_variable.x_max = value.attrib["x_max"]
            model_variable.y_min = value.attrib["y_min"]
            model_variable.y_max = value.attrib["y_max"]

        # check for already cached variable
        model_variable = self._cached_values.get(varname, model_variable)

        self._cached_values[varname] = model_variable

        # only update if not running
        if not self._running_indicator.value:
            self._in_queue.put({"protocol": self.protocol, "vars": self._cached_values})
            self._cached_values = {}

    def _monitor_callback(self, pvname, V) -> None:
        """Callback function used for updating read_only process variables."""
        value = V.raw.value
        varname = self._pvname_to_varname_map[pvname]
        model_variable = self._input_variables[varname]

        if not model_variable:
            model_variable = self._output_variables[varname]

        # check for already cached variable
        model_variable = self._cached_values.get(varname, model_variable)

        if model_variable.variable_type == "image":
            model_variable.x_min = value.attrib["x_min"]
            model_variable.x_max = value.attrib["x_max"]
            model_variable.y_min = value.attrib["y_min"]
            model_variable.y_max = value.attrib["y_max"]

        self._cached_values[varname] = model_variable

        # only update if not running
        if not self._running_indicator.value:
            self._in_queue.put({"protocol": self.protocol, "vars": self._cached_values})
            self._cached_values = {}

    def _initialize_model(self):
        """Initialize model"""

        rep = {"protocol": "pva", "vars": self._input_variables}

        self._in_queue.put(rep)

    def setup_server(self) -> None:
        """Configure and start server."""

        self._context = Context()

        # update value with stored defaults
        for var_name in self._input_variables:
            if self._epics_config[var_name]["serve"]:
                self._input_variables[var_name].value = self._input_variables[
                    var_name
                ].default

            else:

                if self._context is None:
                    self._context = Context("pva")

                try:
                    val = self._context.get(self._varname_to_pvname_map[var_name])
                    val = val.raw.value

                except:
                    self.exit_event.set()
                    raise ValueError(
                        f"Unable to connect to {self._varname_to_pvname_map[var_name]}"
                    )

                self._input_variables[var_name].value = val

        # update output variable values
        self._initialize_model()
        model_outputs = None
        while not self.shutdown_event.is_set() and model_outputs is None:

            try:
                model_outputs = self._out_queue.get(timeout=0.1)
            except Empty:
                pass

        if self.shutdown_event.is_set():
            pass

        # if startup hasn't failed
        else:
            model_output_vars = model_outputs.get("output_variables", {})
            self._output_variables.update(model_output_vars)

            variables = copy.deepcopy(self._input_variables)
            variables.update(self._output_variables)

            # ignore interrupt in subprocess
            signal.signal(signal.SIGINT, signal.SIG_IGN)

            logger.info("Initializing pvAccess server")

            # initialize global inputs
            self._structures = {}
            self._structure_specs = {}
            for variable_name, config in self._epics_config.items():

                if config["serve"]:

                    fields = config.get("fields")
                    pvname = config.get("pvname")

                    if fields is not None:

                        spec = []
                        structure = {}

                        for field in fields:
                            # track fields in dict
                            self._field_to_parent_map[field] = variable_name

                            variable = variables[field]

                            if variable is None:
                                raise ValueError(
                                    f"Field {field} for {variable_name} not found in variable list"
                                )

                            if variable.variable_type == "scalar":
                                spec.append((field, "d"))
                                nt = NTScalar("d")
                                initial = variable.value

                            if variable.variable_type == "table":
                                spec.append((field, "v"))
                                table_rep = ()
                                for col in variable.columns:

                                    # here we assume double type in tables...
                                    table_rep += (col, "ad")

                                nt = NTTable(table_rep)
                                initial = nt.wrap(variable.value)

                            if variable.variable_type == "array":
                                spec.append((field, "v"))

                                if variable.value_type == "str":
                                    nt = NTScalar("s")
                                    initial = variable.value

                                else:
                                    nd_array = variable.value.view(NTNDArrayData)
                                    nt = NTNDArray()
                                    initial = nt.wrap(nd_array)

                            if variable.variable_type == "image":
                                spec.append((field, "v"))

                                nd_array = variable.value.view(NTNDArrayData)
                                nd_array.attrib = {
                                    "x_min": variable.x_min,
                                    "y_min": variable.y_min,
                                    "x_max": variable.x_max,
                                    "y_max": variable.y_max,
                                }

                                nt = NTNDArray()
                                initial = nt.wrap(nd_array)

                            structure[field] = initial

                        # assemble pv
                        self._structures[variable_name] = structure
                        self._structure_specs[variable_name] = spec
                        struct_type = Type(id=variable_name, spec=spec)
                        struct_value = Value(struct_type, structure)
                        pv = SharedPV(initial=struct_value)
                        self._providers[pvname] = pv

                    else:
                        variable = variables[variable_name]
                        # prepare scalar variable types
                        if variable.variable_type == "scalar":
                            nt = NTScalar("d")
                            initial = variable.value

                        # prepare image variable types
                        elif variable.variable_type == "image":
                            nd_array = variable.value.view(NTNDArrayData)
                            nd_array.attrib = {
                                "x_min": variable.x_min,
                                "y_min": variable.y_min,
                                "x_max": variable.x_max,
                                "y_max": variable.y_max,
                            }
                            nt = NTNDArray()
                            initial = nd_array

                        elif variable.variable_type == "table":
                            table_rep = ()
                            for col in variable.columns:

                                # here we assume double type in tables...
                                table_rep += (col, "ad")

                            nt = NTTable(table_rep)
                            initial = nt.wrap(variable.value)

                        elif variable.variable_type == "array":
                            if variable.value_type == "str":
                                nt = NTScalar("as")
                                initial = variable.value

                            else:
                                nd_array = variable.value.view(NTNDArrayData)
                                nt = NTNDArray()
                                initial = nd_array

                        else:
                            raise ValueError(
                                "Unsupported variable type provided: %s",
                                variable.variable_type,
                            )

                        if variable.name in self._input_variables:
                            handler = PVAccessInputHandler(
                                pvname=pvname,
                                is_constant=variable.is_constant,
                                server=self,
                            )

                            pv = SharedPV(handler=handler, nt=nt, initial=initial)

                        else:
                            pv = SharedPV(nt=nt, initial=initial)

                        self._providers[pvname] = pv

                # if not serving pv, set up monitor
                else:
                    if variable.name in self._input_variables:
                        self._monitors[pvname] = self._context.monitor(
                            pvname, partial(self._monitor_callback, pvname)
                        )

                    # in this case, externally hosted output variable
                    else:
                        self._providers[pvname] = None

            if "summary" in self._epics_config:
                pvname = self._epics_config["summary"].get("pvname")
                owner = self._epics_config["summary"].get("owner")
                date_published = self._epics_config["summary"].get("date_published")
                description = self._epics_config["summary"].get("description")
                id = self._epics_config["summary"].get("id")

                spec = [
                    ("id", "s"),
                    ("owner", "s"),
                    ("date_published", "s"),
                    ("description", "s"),
                    ("input_variables", "as"),
                    ("output_variables", "as"),
                ]
                values = {
                    "id": id,
                    "date_published": date_published,
                    "description": description,
                    "owner": owner,
                    "input_variables": [
                        self._epics_config[var]["pvname"]
                        for var in self._input_variables
                    ],
                    "output_variables": [
                        self._epics_config[var]["pvname"]
                        for var in self._input_variables
                    ],
                }

                pv_type = Type(id="summary", spec=spec)
                value = Value(pv_type, values)
                pv = SharedPV(initial=value)
                self._providers[pvname] = pv

            # initialize pva server
            self.pva_server = P4PServer(providers=[self._providers])

            logger.info("pvAccess server started")

    def update_pvs(
        self,
        input_variables: Dict[str, InputVariable],
        output_variables: Dict[str, OutputVariable],
    ) -> None:
        """Update process variables over pvAccess.

        Args:
            input_variables (Dict[str, InputVariable]): Dict of lume-epics output variables.

            output_variables (Dict[str, OutputVariable]): Dict of lume-model output variables.

        """
        variables = input_variables
        variables.update(output_variables)

        for variable in variables.values():
            parent = self._field_to_parent_map.get(variable.name)

            if variable.name in self._input_variables and variable.is_constant:
                logger.debug("Cannot update constant variable.")

            else:
                if variable.variable_type == "image":
                    logger.debug(
                        "pvAccess image process variable %s updated.", variable.name
                    )
                    nd_array = variable.value.view(NTNDArrayData)

                    # get dw and dh from model output
                    nd_array.attrib = {
                        "x_min": variable.x_min,
                        "y_min": variable.y_min,
                        "x_max": variable.x_max,
                        "y_max": variable.y_max,
                    }
                    value = nd_array

                elif variable.variable_type == "array":
                    logger.debug(
                        "pvAccess array process variable %s updated.", variable.name
                    )
                    if variable.value_type == "str":
                        value = variable.value

                    else:
                        value = variable.value.view(NTNDArrayData)

                # do not build attribute pvs
                else:
                    logger.debug(
                        "pvAccess process variable %s updated with value %s.",
                        variable.name,
                        variable.value,
                    )
                    value = variable.value

            # update structure or pv
            if parent:
                self._structures[parent][variable.name] = value
                struct_type = Type(id=parent, spec=self._structure_specs[parent])
                value = Value(struct_type, self._structures[parent])
                pvname = self._varname_to_pvname_map[parent]
                output_provider = self._providers[pvname]

            else:
                pvname = self._varname_to_pvname_map[variable.name]
                output_provider = self._providers[pvname]

            if output_provider:
                output_provider.post(value)

            # in this case externally hosted
            else:
                try:
                    self._context.put(pvname, value)
                except:
                    self.exit_event.set()
                    self.shutdown()

    def run(self) -> None:
        """Start server process."""
        self.setup_server()

        # mark running
        while not self.shutdown_event.is_set():
            try:
                data = self._out_queue.get_nowait()
                inputs = data.get("input_variables", {})
                outputs = data.get("output_variables", {})
                self.update_pvs(inputs, outputs)

                # check cached values
                if len(self._cached_values) > 0 and not self._running_indicator.value:
                    self._in_queue.put(
                        {"protocol": self.protocol, "vars": self._cached_values}
                    )

            except Empty:
                time.sleep(0.1)
                logger.debug("out queue empty")

        self._context.close()
        if self.pva_server is not None:
            self.pva_server.stop()

        logger.info("pvAccess server stopped.")

    def shutdown(self):
        """Safely shutdown the server process."""
        self.shutdown_event.set()


class PVAccessInputHandler:
    """
    Handler object that defines the callbacks to execute on put operations to input
    process variables.
    """

    def __init__(self, pvname: str, is_constant: bool, server: PVAServer):
        """
        Initialize the handler with prefix and image pv attributes

        Args:
            pvname (str): The PV being handled
            is_constant (bool): Indicator of constant variable
            server (PVAServer): Reference to the server holding this PV

        """
        self.is_constant = is_constant
        self.pvname = pvname
        self.server = server

    def put(self, pv: SharedPV, op: ServOpWrap) -> None:
        """Updates the global input process variable state, posts the input process
        variable value change, runs the thread local BaseModel instance
        using the updated global input process variable states, and posts the model
        output values to the output process variables.

        Args:
            pv (SharedPV): Input process variable on which the put operates.

            op (ServOpWrap): Server operation initiated by the put call.

        """
        # update input values and global input process variable state
        if not self.is_constant and op.value() is not None:
            pv.post(op.value())
            self.server.update_pv(pvname=self.pvname, value=op.value())
        # mark server operation as complete
        op.done()
