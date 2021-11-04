import logging
import multiprocessing
from multiprocessing.managers import DictProxy
from queue import Full, Empty
import numpy as np
import time
import signal
from typing import List, Union
from functools import partial
from lume_model.variables import InputVariable, OutputVariable
from p4p.client.thread import Context
from p4p.nt import NTScalar, NTNDArray
from p4p.server.thread import SharedPV
from p4p.server import Server as P4PServer
from p4p.nt.ndarray import ntndarray as NTNDArrayData
from p4p.server.raw import ServOpWrap

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
        exit_event (multiprocessing.Event): Event indicating shutdown
        _prefix (str): Process variable prefix string
        _input_variables (List[InputVariable]): List of input variables
        _output_variables (List[OutputVariable]): List of output variables
        _in_queue (multiprocessing.Queue): input variable queue
        _out_queue (multiprocessing.Queue): output variable update queue
        _providers (dict): Dictionary mapping pvname to p4p provider
        _running_indicator (multiprocessing.Value): Boolean indicator of running model execution
        _read_only (bool): Boolean indicator of whether read-only server
        _monitors (dict): Dictionary of monitor objects for read-only server
        _cached_values (dict): Dict for caching values while model executes

    """

    protocol = "pva"

    def __init__(
        self,
        prefix: str,
        input_variables: List[InputVariable],
        output_variables: List[OutputVariable],
        in_queue: multiprocessing.Queue,
        out_queue: multiprocessing.Queue,
        running_indicator: multiprocessing.Value,
        read_only: bool = False,
        *args,
        **kwargs,
    ) -> None:
        """Initialize server process.

        Args:
            prefix (str): EPICS prefix for serving process variables

            input_variables (Dict[str, InputVariable]): Dictionary mapping pvname to lume-model input variable.

            output_variables (Dict[str, OutputVariable]):Dictionary mapping pvname to lume-model output variable.

            in_queue (multiprocessing.Queue): Queue for tracking updates to input variables

            out_queue (multiprocessing.Queue): Queue for tracking updates to output variables

            running_indicator (multiprocessing.Value): Boolean indicator indicating running model execution

            read_only (bool): Boolean indicator of whether read-only server

        """

        super().__init__(*args, **kwargs)
        self.pva_server = None
        self.exit_event = multiprocessing.Event()
        self._prefix = prefix
        self._input_variables = input_variables
        self._output_variables = output_variables
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._providers = {}
        self._running_indicator = running_indicator
        self._read_only = read_only
        # monitors for read only
        self._monitors = {}
        self._cached_values = {}

    def update_pv(self, pvname: str, value: Union[np.ndarray, float]) -> None:
        """Adds update to input process variable to the input queue.

        Args:
            pvname (str): Name of process variable

            value (Union[np.ndarray, float]): Value to set

        """
        # Hack for now to get the pickable value
        value = value.raw.value
        pvname = pvname.replace(f"{self._prefix}:", "")

        self._cached_values.update({pvname: val})

        # only update if not running
        if not self._running_indicator.value:
            self._in_queue.put({"protocol": self.protocol, "pvs": self._cached_values})
            self._cached_values = {}

    def _monitor_callback(self, pvname, V) -> None:
        """Callback function used for updating read_only process variables.

        """
        val = V.raw.value
        pvname = pvname.replace(f"{self._prefix}:", "")

        self._cached_values.update({pvname: val})

        # only update if not running
        if not self._running_indicator.value:
            self._in_queue.put({"protocol": self.protocol, "pvs": self._cached_values})
            self._cached_values = {}

    def setup_server(self) -> None:
        """Configure and start server.

        """
        # initialize context
        if self._read_only:
            self._context = Context("pva")

        # ignore interrupt in subprocess
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        logger.info("Initializing pvAccess server")
        # initialize global inputs

        for variable in self._input_variables.values():
            pvname = f"{self._prefix}:{variable.name}"

            if not self._read_only:

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
                        "Unsupported variable type provided: %s", variable.variable_type
                    )

                handler = PVAccessInputHandler(
                    pvname=pvname, is_constant=variable.is_constant, server=self
                )
                pv = SharedPV(handler=handler, nt=nt, initial=initial)
                self._providers[pvname] = pv

            else:
                self._monitors[pvname] = self._context.monitor(
                    pvname, partial(self._monitor_callback, pvname)
                )

        # use default handler for the output process variables
        # updates to output pvs are handled from post calls within the input
        # update
        for variable in self._output_variables.values():
            pvname = f"{self._prefix}:{variable.name}"
            if variable.variable_type == "scalar":
                nt = NTScalar()
                initial = variable.value

            elif variable.variable_type == "image":
                nd_array = variable.value.view(NTNDArrayData)

                # get image limits from model output
                nd_array.attrib = {
                    "x_min": np.float64(variable.x_min),
                    "y_min": np.float64(variable.y_min),
                    "x_max": np.float64(variable.x_max),
                    "y_max": np.float64(variable.y_max),
                }

                nt = NTNDArray()
                initial = nd_array

            elif variable.variable_type == "array":

                if variable.value_type == "string":
                    nt = NTScalar("as")
                    initial = variable.value

                else:
                    nd_array = variable.value.view(NTNDArrayData)
                    nt = NTNDArray()
                    initial = nd_array
            else:
                raise ValueError(
                    "Unsupported variable type provided: %s", variable.variable_type
                )

            pv = SharedPV(nt=nt, initial=initial)
            self._providers[pvname] = pv

        else:
            pass  # throw exception for incorrect data type

        # initialize pva server
        self.pva_server = P4PServer(providers=[self._providers])

        logger.info("pvAccess server started")

    def update_pvs(
        self,
        input_variables: List[InputVariable],
        output_variables: List[OutputVariable],
    ) -> None:
        """Update process variables over pvAccess.

        Args:
            input_variables (List[InputVariable]): List of lume-epics output variables.

            output_variables (List[OutputVariable]): List of lume-model output variables.

        """
        variables = input_variables + output_variables
        for variable in variables:

            if variable.name in self._input_variables and variable.is_constant:
                logger.debug("Cannot update constant variable.")

            else:
                pvname = f"{self._prefix}:{variable.name}"
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
                    if variable.value_type == "string":
                        value = list(variable.value)

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

            output_provider = self._providers[pvname]
            output_provider.post(value)

    def run(self) -> None:
        """Start server process.

        """
        self.setup_server()

        # mark running
        while not self.exit_event.is_set():
            try:
                data = self._out_queue.get_nowait()
                inputs = data.get("input_variables", [])
                outputs = data.get("output_variables", [])
                self.update_pvs(inputs, outputs)

                # check cached values
                if len(self._cached_values) > 0 and not self._running_indicator.value:
                    self._in_queue.put(
                        {"protocol": self.protocol, "pvs": self._cached_values}
                    )

            except Empty:
                time.sleep(0.1)
                logger.debug("out queue empty")

        self._context.close()
        self.pva_server.stop()
        logger.info("pvAccess server stopped.")

    def shutdown(self):
        """Safely shutdown the server process.

        """
        self.exit_event.set()


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
            server (PVAServer): Reference to the server holding this PV

        """
        self.is_constant = is_constant
        self.pvname = pvname
        self.server = server

    def put(self, pv: SharedPV, op: ServOpWrap) -> None:
        """Updates the global input process variable state, posts the input process
        variable value change, runs the thread local OnlineSurrogateModel instance
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
