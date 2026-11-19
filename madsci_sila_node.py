"""REST-based Node Module helper classes."""

import inspect
import json
import re
import tempfile
import types
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from typing import (
    Any,
    Callable,
    get_args,
    get_origin,
    get_type_hints,
)
from uuid import uuid4
from zipfile import ZipFile

from madsci.common.ownership import global_ownership_info
from madsci.common.types.action_types import (
    ActionFiles,
    ArgumentDefinition
)
from madsci.common.types.base_types import MadsciBaseModel
from madsci.common.types.location_types import LocationArgument
from madsci.common.types.node_types import (
    RestNodeConfig,
)

from madsci.node_module.abstract_node_module import (
    AbstractNode,
)
from pydantic import AnyUrl, ValidationError
from pydantic_core import to_jsonable_python
from sila2.framework import Feature
from sila2.server import FeatureImplementationBase, SilaServer

sila_basic_datatypes = {
    "str": "String",
    "int": "Integer",
    "float": "Real",
    "bool": "Boolean",
    "Path": "Binary",
    "datetime.time": "Time",
    "datetime.date": "Date",
    "datetime.datetime": "Timestamp",
}


class SubServer(SilaServer):
    """Sila Server to run in Sila mode"""

    def __init__(self, name: str, description: str, type: str) -> None:
        """create a Sila Server with MADSci info"""
        super().__init__(
            server_name=name,
            server_description=description,
            server_type=type,
            server_version="0.1.0",
            server_vendor_url="https://github.com/AD-SDL/MADSci",
            server_uuid=uuid4(),
        )


class SilaNode(AbstractNode):
    """SiLA-based node implementation with better OpenAPI documentation and result handling."""

    """The default supported capabilities of this node module class."""
    config: RestNodeConfig = RestNodeConfig()
    """The configuration for the node."""
    config_model = RestNodeConfig
    """The node config model class. This is the class that will be used to instantiate self.config."""

    def __init__(self, *args: Any, **kwargs: Any) -> "SilaNode":
        """Initialize the node class."""
        super().__init__(*args, **kwargs)
        self.node_info.node_url = getattr(self.config, "node_url", None)
        self._action_result_models = {}  # Cache for dynamic result models
        node_type = getattr(self.node_info.node_type, "value", self.node_info.node_type)
        if self.node_info.node_description is None:
            self.node_info.node_description = (
                f"A node for {self.node_info.node_name} of type {node_type}"
            )
        self.construct_sila_class(
            self.node_info.node_name,
            self.node_info.node_description,
            f"MADSCI{node_type}",
        )


    def start_node(self, testing: bool = False) -> None:
        """Start the node."""
        global_ownership_info.node_id = self.node_info.node_id
        url = AnyUrl(getattr(self.config, "node_url", "http://127.0.0.1:2000"))

        # Create FastAPI app metadata from node info
        host = url.host
        port = url.port
        super().start_node()
        self.server.start_insecure(host, port)
        self.server.grpc_server.wait_for_termination()
        self.server.stop()


    def process_sila_arguments(self, argument: Any) -> Any:
        """Process an incoming arguments from the sila client"""
        if isinstance(argument, bytes):
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(argument)
                temp_file.flush()
                final_arg = Path(temp_file.name)
        elif isinstance(argument, str):
            try:
                possible_location = json.loads(argument)
                loc_arg = LocationArgument.model_validate(possible_location)
                final_arg = loc_arg
            except json.JSONDecodeError:
                final_arg = argument
            except ValidationError:
                final_arg = argument
        else:
            final_arg = argument
        return final_arg

    def generator(self, action: Callable) -> Callable:
        """generates handler functions from MADSci actions"""
        outer_self = self

        def new_function(self, *args: Any, **kwargs: dict[str, Any]) -> Any:  # noqa: ARG001, ANN001
            """Wraps the action function to process arguments and handle return types."""
            kwargs.pop("metadata", None)
            new_args = ()
            for i in range(len(args)):
                new_args += (outer_self.process_sila_arguments(args[i]),)
            new_args = (outer_self, *new_args)
            result = action(*new_args, **kwargs)
            return_hint = get_type_hints(action).get("return")
            if result is None or return_hint in (None, type(None)):
                # Commands without a Response must return nothing
                return None
            if return_hint == Path:
                with Path.open(result, "rb") as f:
                    return f.read()
            if getattr(return_hint, "__name__", None) in sila_basic_datatypes:
                return result
            if isinstance(result, ActionFiles):
                # Multiple files - add all to ZIP with their labels as names
                with (
                    tempfile.NamedTemporaryFile(
                        suffix=".zip", delete=False
                    ) as temp_zip_file,
                    ZipFile(temp_zip_file.name, "w") as zip_file,
                ):
                    for file_label, file_path in result.model_dump().items():
                        if Path(file_path).exists():
                            # Use the file label as the filename in the ZIP
                            file_extension = Path(file_path).suffix
                            zip_filename = f"{file_label}{file_extension}"
                            zip_file.write(file_path, zip_filename)
                with Path.open(Path(temp_zip_file.name), "rb") as f:
                    return f.read()
            if isinstance(result, MadsciBaseModel):
                return json.dumps(result.model_dump(mode="json"))
            return json.dumps(to_jsonable_python(result))

        return new_function

    def construct_sila_class(self, name: str, description: str, type: str) -> None:
        """Construct a dynamic SilaClass with action handlers."""
        xml_str = self.generate_feature_xml()
        feature = Feature(xml_str)
        keywords = {}
        for action_name, action in self.action_handlers.items():
            new_handler = self.generator(action)
            cleaned = re.sub(r"[^A-Za-z0-9]", "", action_name)
            keywords["MADSCI" + cleaned] = new_handler
        outer_self = self

        def wrapped_state(self, *args: Any, **kwargs: dict[str, Any]) -> str:  # noqa: ARG001, ANN001
            return json.dumps(outer_self.node_state)

        keywords["get_MADSCI" + "GetState"] = wrapped_state
        sila_class = types.new_class(
            "SilaClass",
            (FeatureImplementationBase,),
            exec_body=lambda ns: ns.update(keywords),
        )
        self.server = SubServer(name, description, type)
        new_class = sila_class(self.server)
        self.server.set_feature_implementation(feature, new_class)


    


    

   

    def handle_complex_type(
        self, command_elem: ET.Element, parameter: ArgumentDefinition, type_hint: Any
    ) -> None:
        """Add a SiLA parameter for an argument whose type isn't a SiLA basic type.

        Lists of basic types become SiLA Lists; anything else becomes a String.
        """
        parameter_elem = ET.SubElement(command_elem, "Parameter")
        parameter_id = ET.SubElement(parameter_elem, "Identifier")
        parameter_id.text = "MADSCI" + re.sub(r"[^A-Za-z0-9]", "", parameter.name)
        parameter_name_elem = ET.SubElement(parameter_elem, "DisplayName")
        parameter_name_elem.text = parameter.name
        parameter_description = ET.SubElement(parameter_elem, "Description")
        parameter_description.text = parameter.description
        parameter_type = ET.SubElement(parameter_elem, "DataType")
        list_args = get_args(type_hint) if get_origin(type_hint) is list else ()
        sub_type = getattr(list_args[0], "__name__", None) if list_args else None
        if sub_type in sila_basic_datatypes:
            parameter_list = ET.SubElement(parameter_type, "List")
            list_type = ET.SubElement(parameter_list, "DataType")
            basic = ET.SubElement(list_type, "Basic")
            basic.text = sila_basic_datatypes[sub_type]
        else:
            basic = ET.SubElement(parameter_type, "Basic")
            basic.text = "String"

    def _add_response_to_xml(self, command_elem: ET.Element, action: Callable) -> None:
        """Add the SiLA Response for an action's return value to a command."""
        command_results = ET.SubElement(command_elem, "Response")
        result_id = ET.SubElement(command_results, "Identifier")
        result_id.text = "RESULT"
        result_name = ET.SubElement(command_results, "DisplayName")
        result_name.text = "result"
        result_description = ET.SubElement(command_results, "Description")
        result_description.text = "result of the action"
        result_type = ET.SubElement(command_results, "DataType")
        result_hint = get_type_hints(action).get("return")
        result_basic = ET.SubElement(result_type, "Basic")
        result_type_name = getattr(result_hint, "__name__", None)
        if result_type_name in sila_basic_datatypes:
            result_basic.text = sila_basic_datatypes[result_type_name]
        elif inspect.isclass(result_hint) and issubclass(result_hint, ActionFiles):
            result_basic.text = "Binary"
        else:
            result_basic.text = "String"

    def add_actions_to_xml(self, root: ET.Element) -> ET.Element:
        """Add actions to the XML representation of the node."""
        for action_name, action in self.action_handlers.items():
            command_elem = ET.SubElement(root, "Command")
            command_id = ET.SubElement(command_elem, "Identifier")
            command_id.text = "MADSCI" + re.sub(r"[^A-Za-z0-9]", "", action_name)
            command_name = ET.SubElement(command_elem, "DisplayName")
            command_name.text = action_name
            command_description = ET.SubElement(command_elem, "Description")
            command_description.text = self.node_info.actions[action_name].description
            command_observable = ET.SubElement(command_elem, "Observable")
            command_observable.text = "No"
            for parameter_name in [
                param for param in get_type_hints(action) if param != "return"
            ]:
                if parameter_name in self.node_info.actions[action_name].args:
                    parameter = self.node_info.actions[action_name].args[parameter_name]
                    if parameter.argument_type in sila_basic_datatypes:
                        param_type = sila_basic_datatypes[parameter.argument_type]
                    else:
                        self.handle_complex_type(
                            command_elem,
                            parameter,
                            get_type_hints(action)[parameter_name],
                        )
                        continue
                elif parameter_name in self.node_info.actions[action_name].files:
                    parameter = self.node_info.actions[action_name].files[
                        parameter_name
                    ]
                    param_type = "Binary"
                elif parameter_name in self.node_info.actions[action_name].locations:
                    parameter = self.node_info.actions[action_name].locations[
                        parameter_name
                    ]
                    param_type = "String"

                parameter_elem = ET.SubElement(command_elem, "Parameter")
                parameter_id = ET.SubElement(parameter_elem, "Identifier")
                parameter_id.text = "MADSCI" + re.sub(
                    r"[^A-Za-z0-9]", "", parameter_name
                )
                parameter_name_elem = ET.SubElement(parameter_elem, "DisplayName")
                parameter_name_elem.text = parameter_name
                parameter_description = ET.SubElement(parameter_elem, "Description")
                parameter_description.text = parameter.description
                parameter_type = ET.SubElement(parameter_elem, "DataType")
                parameter_basic = ET.SubElement(parameter_type, "Basic")
                parameter_basic.text = param_type
            if len(self.node_info.actions[action_name].results) > 0:
                self._add_response_to_xml(command_elem, action)
        return root

    def generate_feature_xml(self) -> str:
        """Generate the SiLA XML representation of the node"""
        root = ET.Element(
            "Feature",
            attrib={
                "xmlns": "http://www.sila-standard.org",
                "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "Category": "examples",
                "FeatureVersion": "1.0",
                "MaturityLevel": "Draft",
                "Originator": "org.madsci",
                "SiLA2Version": "1.0",
                "xsi:schemaLocation": "http://www.sila-standard.org https://gitlab.com/SiLA2/sila_base/raw/master/schema/FeatureDefinition.xsd",
            },
        )
        id = ET.SubElement(root, "Identifier")
        id.text = "MADSCI" + self.node_info.node_id
        name = ET.SubElement(root, "DisplayName")
        name.text = self.node_info.node_name
        description = ET.SubElement(root, "Description")
        description.text = self.node_info.node_description
        property_elem = ET.SubElement(root, "Property")
        property_id = ET.SubElement(property_elem, "Identifier")
        property_id.text = "MADSCIGetState"
        property_name = ET.SubElement(property_elem, "DisplayName")
        property_name.text = "MADSCIGetState"
        property_description = ET.SubElement(property_elem, "Description")
        property_description.text = "The MADSci defined state of the node"
        property_observable = ET.SubElement(property_elem, "Observable")
        property_observable.text = "No"
        property_type = ET.SubElement(property_elem, "DataType")
        property_basic = ET.SubElement(property_type, "Basic")
        property_basic.text = "String"
        root = self.add_actions_to_xml(root)

        return ET.tostring(root).decode("utf-8")


if __name__ == "__main__":
    SilaNode().start_node()
