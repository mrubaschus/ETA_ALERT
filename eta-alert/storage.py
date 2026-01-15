import json
import os
from pathlib import Path
from typing import Any, Optional


# NOTE:
# Samsara "persistent-storage" template provides durable storage across runs.
# In this repo, we implement a simple JSON-backed KV store so you can:
# - test locally
# - run in environments where a persistent filesystem path is provided
#
# In Samsara Functions, set FUNCTION_STORAGE_PATH to a durable path if needed.

_DEFAULT_STORAGE_PATH = os.getenv(
    "FUNCTION_STORAGE_PATH",
    str(Path(__file__).with_name(".function_storage.json")),
)


def _load_all(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # If storage is corrupted, fail safe by treating as empty.
        return {}


def _save_all(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def get_item(key: str, *, storage_path: str = _DEFAULT_STORAGE_PATH) -> Optional[dict[str, Any]]:
    data = _load_all(storage_path)
    value = data.get(key)
    return value if isinstance(value, dict) else None


def set_item(key: str, value: dict[str, Any], *, storage_path: str = _DEFAULT_STORAGE_PATH) -> None:
    data = _load_all(storage_path)
    data[key] = value
    _save_all(storage_path, data)


def delete_item(key: str, *, storage_path: str = _DEFAULT_STORAGE_PATH) -> None:
    data = _load_all(storage_path)
    if key in data:
        del data[key]
        _save_all(storage_path, data)
