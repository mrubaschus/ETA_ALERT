"""AWS Lambda / Samsara Functions entrypoint shim.

The application code lives in the `eta-alert/` folder (historical reason). That folder
name contains a dash, so it can't be imported as a normal Python package.

This module exists so the hosted runtime can use handler `main.main`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


_ETA_ALERT_DIR = Path(__file__).parent / "eta-alert"
_ETA_ALERT_MAIN_PY = _ETA_ALERT_DIR / "main.py"


def _load_impl_module():
    # Ensure `storage.py` and other sibling imports resolve from `eta-alert/`.
    sys.path.insert(0, str(_ETA_ALERT_DIR))

    spec = importlib.util.spec_from_file_location("eta_alert_impl", _ETA_ALERT_MAIN_PY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load implementation module at {_ETA_ALERT_MAIN_PY}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_IMPL = None


def main(event: Any = None, context: Any = None):
    """Hosted handler: `main.main`.

    Delegates to `eta-alert/main.py:main`.
    """

    global _IMPL
    if _IMPL is None:
        _IMPL = _load_impl_module()

    # The implementation already accepts (event, context) but doesn't require them.
    return _IMPL.main(event=event, context=context)
