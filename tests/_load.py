"""Load the plugin as a package (as Hermes does) against the test stubs."""

import importlib.util
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
PKG = "meshcore_platform"

if str(TESTS / "stubs") not in sys.path:
    sys.path.insert(0, str(TESTS / "stubs"))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


def load():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


pkg = load()
import importlib as _il  # noqa: E402

protocol = _il.import_module(f"{PKG}.protocol")
repeater = _il.import_module(f"{PKG}.repeater")
state = _il.import_module(f"{PKG}.state")
textutil = _il.import_module(f"{PKG}.textutil")
registry = _il.import_module(f"{PKG}.registry")
adapter = _il.import_module(f"{PKG}.adapter")
tools = _il.import_module(f"{PKG}.tools")
