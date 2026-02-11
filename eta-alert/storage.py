import json
import os
from pathlib import Path
from typing import Any, Optional


def _is_serverless_readonly_task_root() -> bool:
    # AWS Lambda / Lambda-like environments mount the code package at /var/task (read-only).
    # Samsara Functions is Lambda-like.
    return bool(
        os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        or os.getenv("LAMBDA_TASK_ROOT")
        or os.getenv("AWS_EXECUTION_ENV")
    )


def _default_storage_path() -> str:
    # NOTE:
    # Samsara "persistent-storage" template provides durable storage across runs.
    # In this repo, we implement a simple JSON-backed KV store so you can:
    # - test locally
    # - run in environments where a persistent filesystem path is provided
    #
    # In serverless runtimes the deployment directory is read-only; use temp storage by default.
    # This is best-effort dedupe (may be lost on cold start). For durable dedupe,
    # set FUNCTION_STORAGE_PATH to a durable location or use the persistent-storage template.
    explicit = os.getenv("FUNCTION_STORAGE_PATH")
    if explicit:
        return explicit

    if _is_serverless_readonly_task_root():
        temp_root = (
            os.getenv("SamsaraFunctionTempStoragePath")
            or os.getenv("TMPDIR")
            or os.getenv("TEMP")
            or "/tmp"
        )
        return str(Path(temp_root) / ".function_storage.json")

    return str(Path(__file__).with_name(".function_storage.json"))


def _resolve_storage_path(storage_path: Optional[str]) -> str:
    return storage_path or _default_storage_path()


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


def get_item(key: str, *, storage_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    path = _resolve_storage_path(storage_path)
    data = _load_all(path)
    value = data.get(key)
    return value if isinstance(value, dict) else None


def set_item(key: str, value: dict[str, Any], *, storage_path: Optional[str] = None) -> None:
    path = _resolve_storage_path(storage_path)
    data = _load_all(path)
    data[key] = value
    _save_all(path, data)


def delete_item(key: str, *, storage_path: Optional[str] = None) -> None:
    path = _resolve_storage_path(storage_path)
    data = _load_all(path)
    if key in data:
        del data[key]
        _save_all(path, data)
