"""
The lume-epics controller serves as the intermediary between variable monitors 
and process variables served over EPICS.
"""
from typing import Union
import numpy as np
import copy
import logging
from collections import defaultdict
from epics import caget, caput, PV
from p4p.client.thread import Context

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_DATA = {
    "image": [np.zeros((50, 50))],
    "x": [50],
    "y": [50],
    "dw": [0.01],
    "dh": [0.01],
}

DEFAULT_SCALAR_VALUE = 0


class Controller:
    """
    Controller class used to access process variables. Controllers are used for 
    interfacing with both Channel Access and pvAccess process variables. The 
    controller object is initialized using a single protocol has methods for
    both getting and setting values on the process variables.

    Attributes:
        protocol (str): Protocol for getting values from variables ("pva" for pvAccess, "ca" for
            Channel Access)

        context (Context): P4P threaded context instance for use with pvAccess.

        set_ca (bool): Update Channel Access variable on put.

        set_pva (bool): Upddate pvAccess variable on put.

    Example:
        ```
        # create PVAcess controller
        controller = Controller("pva")

        value = controller.get_value("scalar_input")
        image_value = controller.get_image("image_input")

        controller.close()

        ```

    """

    def __init__(self, protocol: str):
        """
        Initializes controller. Stores protocol and creates context attribute if 
        using pvAccess.

        Args: 
            protocol (str): Protocol for getting values from variables ("pva" for pvAccess, "ca" for
            Channel Access)

        """
        self.protocol = protocol
        self.pv_registry = defaultdict()

        # initalize context for pva
        self.context = None
        if self.protocol == "pva":
            self.context = Context("pva")


    def ca_value_callback(self, pvname, value, *args, **kwargs):
        self.pv_registry[pvname]["value"] = value

    def pva_value_callback(self, pvname, value):
        self.pv_registry[pvname]["value"] = value

    def setup_pv_monitor(self, pvname):
        if pvname in self.pv_registry:
            return

        if self.protocol == "ca":
            pv_obj = PV(pvname, callback=self.ca_value_callback)
            self.pv_registry[pvname] = {'pv': pv_obj, 'value': None}

        elif self.protocol == "pva":
            cb = functools.partial(self.pva_value_callback, pvname)
            mon_obj = self.context.monitor(pvname, cb)
            self.pv_registry[pvname] = {'pv': mon_obj, 'value': None}

    def get(self, pvname: str) -> np.ndarray:
        """
        Accesses and returns the value of a process variable.

        Args:
            pvname (str): Process variable name
        """
        self.setup_pv_monitor(pvname)
        pv = self.pv_registry.get(pvname, None)
        if pv:
            return pv.get('value', None)
        return None


    def get_value(self, pvname):
        """Gets scalar value of a process variable.

        Args:
            pvname (str): Image process variable name.

        """
        value = self.get(pvname)

        if value is None:
            value = DEFAULT_SCALAR_VALUE

        return value

    def get_image(self, pvname) -> dict:
        """Gets image data via controller protocol.

        Args:
            pvname (str): Image process variable name

        """

        if self.protocol == "ca":
            image = self.get(f"{pvname}:ArrayData_RBV")

            if image is not None:
                pvbase = pvname.replace(":ArrayData_RBV", "")
                nx = self.get(f"{pvbase}:ArraySizeX_RBV")
                ny = self.get(f"{pvbase}:ArraySizeY_RBV")
                x = self.get(f"{pvbase}:MinX_RBV")
                y = self.get(f"{pvbase}:MinY_RBV")
                dw = self.get(f"{pvbase}:MaxX_RBV") - x
                dh = self.get(f"{pvbase}:MaxY_RBV") - y

                image = image.reshape(int(nx), int(ny))

        elif self.protocol == "pva":
            # context returns numpy array with WRITEABLE=False
            # copy to manipulate array below
            image = self.get(pvname)

            if image is not None:
                attrib = image.attrib
                x = attrib["x_min"]
                y = attrib["y_min"]
                dw = attrib["x_max"] - attrib["x_min"]
                dh = attrib["y_max"] - attrib["y_min"]
                image = copy.copy(image)

        if image is not None:
            return {
                "image": [image],
                "x": [x],
                "y": [y],
                "dw": [dw],
                "dh": [dh],
            }

        else:
            return DEFAULT_IMAGE_DATA


    def put(self, pvname, value: Union[np.ndarray, float]) -> None:
        """Assign the value of a process variable.

        Args:
            pvname (str): Name of the process variable

            value (Union[np.ndarray, float]): Value to assing to process variable.

        """
        if self.protocol == "ca":
            caput(pvname, value)

        elif self.protocol == "pva":
            self.context.put(pvname, value, throw=False)

    def close(self):
        if self.protocol == "pva":
            self.context.close()
