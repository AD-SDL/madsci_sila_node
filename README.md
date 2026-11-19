# madsci_sila_node

A [MADSci](https://github.com/AD-SDL/MADSci) node module that exposes a node's actions over [SiLA 2](https://sila-standard.com/).

The `SilaNode` can be used to replace `RestNode` in any MADSci node. This is a side project and we do not guarantee full functionality or updates, but hope that it provides a useful starting place for anyone interested. 

`SilaNode` subclasses MADSci's `AbstractNode`. When the node starts, it reads the node's action definitions and builds a SiLA Feature Definition (XML) from them. It then serves that feature from a SiLA 2 server, so any SiLA 2 client can discover and call the node's actions.

## Installation

```bash
pip install -e .
```

Or, for development:

```bash
pip install -e ".[dev]"
```

## Usage



Subclass `SilaNode` and define actions the same way you would for any MADSci node:

```python
from pathlib import Path

from madsci.node_module.helpers import action
from madsci_sila_node import SilaNode


class MyInstrumentNode(SilaNode):
    @action
    def measure(self, sample_name: str, repeats: int = 1) -> float:
        """Take a measurement of a sample."""
        ...

    @action
    def export_data(self, run_id: str) -> Path:
        """Export the data file for a run."""
        ...


if __name__ == "__main__":
    MyInstrumentNode().start_node()
```

The SiLA server binds to the host and port in the node's `node_url` config value (default `sila://127.0.0.1:2000`).

## How MADSci concepts map to SiLA

| MADSci | SiLA 2 |
| --- | --- |
| Node | A single Feature, with identifier `MADSCI<node_id>` |
| Action | Unobservable Command, with identifier `MADSCI<action_name>` (non-alphanumeric characters removed) |
| Node state | Unobservable Property `MADSCIGetState` (JSON string) |
| Action result | Command Response `RESULT` |

### Argument types

| Python type | SiLA type |
| --- | --- |
| `str` | String |
| `int` | Integer |
| `float` | Real |
| `bool` | Boolean |
| `Path` / file arguments | Binary |
| `datetime.time` / `date` / `datetime` | Time / Date / Timestamp |
| `list[<basic type>]` | List of the matching basic type |
| `LocationArgument` | String (JSON-encoded `LocationArgument`) |
| Anything else | String |

Binary arguments are written to a temporary file and passed to the action as a `Path`. String arguments that parse as a JSON `LocationArgument` are converted to one.

### Return types

| Python return type | SiLA response |
| --- | --- |
| `None` | No response |
| Basic types above | Matching basic type |
| `Path` | Binary (file contents) |
| `ActionFiles` | Binary (ZIP archive; each file is named by its label) |
| `MadsciBaseModel` or other values | String (JSON) |

## License

See the [MADSci repository](https://github.com/AD-SDL/MADSci) for licensing information.
