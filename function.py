"""Samsara Functions entrypoint shim (handler: function.main).

Some Samsara Functions deployments use `function.main` as the handler name
instead of `main.main`. This module simply re-exports the same handler
so both handler names work.
"""

from main import main  # noqa: F401

__all__ = ["main"]
