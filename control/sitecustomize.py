"""Environment-scoped Studio extension for the independent KT client."""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent
_CONFIG = os.environ.get("KT_STUDIO_CONTROL_CONFIG")
_MODULE = "kohakuterrarium.api.mcp_tools"


class _ControlLoader(importlib.abc.Loader):
    def __init__(self, loader, config_path):
        self.loader = loader
        self.config_path = config_path

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        from mcp_extension import configure_factory
        configure_factory(module, self.config_path)


class _ControlFinder(importlib.abc.MetaPathFinder):
    def __init__(self, config_path):
        self.config_path = config_path

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _MODULE:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            raise ImportError("Configured Studio control requires the installed KT MCP app")
        spec.loader = _ControlLoader(spec.loader, self.config_path)
        return spec


if _CONFIG:
    # The launcher supplies a per-device config under its own data/control
    # directory. Never accept the production Bridge control tree here.
    config_path = Path(_CONFIG).expanduser().resolve()
    if (
        config_path.name != "control.json"
        or config_path.parent.name.casefold() != "control"
        or not config_path.is_file()
        or "kt-mcp-bridge" in str(config_path).casefold()
    ):
        raise RuntimeError("Invalid independent Studio control configuration path")
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    sys.meta_path.insert(0, _ControlFinder(config_path))
