"""Load the bundled Studio extension only for an independent KT child.

The portable Python runtime uses a ``._pth`` file and therefore ignores the
process ``PYTHONPATH`` during path construction. This small shim lives on the
already bundled ``src`` path, then delegates to the copied extension in the
project's ``control`` directory when the client explicitly supplies its
per-device control configuration.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def _load_independent_extension() -> None:
    config = os.environ.get("KT_STUDIO_CONTROL_CONFIG", "").strip()
    if not config:
        return
    project_root = Path(__file__).resolve().parents[1]
    control_root = (project_root / "control").resolve(strict=False)
    if "kt-mcp-bridge" in str(control_root).casefold():
        raise RuntimeError("Independent Studio extension resolved to production Bridge")
    module_path = control_root / "sitecustomize.py"
    if not module_path.is_file():
        raise RuntimeError("Independent Studio extension is missing")
    if str(control_root) not in sys.path:
        sys.path.insert(0, str(control_root))
    spec = importlib.util.spec_from_file_location("_kt_independent_sitecustomize", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Independent Studio extension cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


_load_independent_extension()

