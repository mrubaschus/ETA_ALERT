"""KV storage — Samsara persistent Database in hosted mode, local JSON fallback.

When running inside Samsara Functions (SamsaraFunctionStorageName env var is set),
uses the official `samsarafnstorage.Database` backed by S3 so state survives cold
starts.  Locally (no env var), falls back to a JSON file next to this script.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Samsara persistent Database (S3-backed, survives cold starts)
# ---------------------------------------------------------------------------


def _use_samsara_storage() -> bool:
    """True when running inside Samsara Functions with storage configured."""
    return bool(os.getenv("SamsaraFunctionStorageName"))


def _get_samsara_db(force_refresh=False):
    """Get a Database instance, optionally refreshing credentials."""
    try:
        from samsarafnstorage import get_database
        return get_database("eta-alert", force_refresh=force_refresh)
    except Exception as exc:
        print(f"[WARN] Failed to init Samsara Database: {type(exc).__name__}: {exc}")
        return None


def _is_expired_token_error(exc: Exception) -> bool:
    """Check if exception is an AWS ExpiredToken error."""
    exc_str = str(exc)
    return "ExpiredToken" in exc_str or "expired" in exc_str.lower()


# ---------------------------------------------------------------------------
# Local JSON fallback (development / non-Samsara runtimes)
# ---------------------------------------------------------------------------

def _default_storage_path() -> str:
    explicit = os.getenv("FUNCTION_STORAGE_PATH")
    if explicit:
        return explicit
    # In Lambda /var/task is read-only — use /tmp for the fallback file
    if os.path.isdir("/tmp"):
        return "/tmp/.function_storage.json"
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
        return {}


def _save_all(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API  (unchanged signatures — get_item / set_item / delete_item)
# ---------------------------------------------------------------------------

def get_item(key: str, *, storage_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    if _use_samsara_storage():
        for attempt in range(2):
            db = _get_samsara_db(force_refresh=(attempt > 0))
            if db is not None:
                try:
                    val = db.get_dict(key)
                    print(f"[storage] GET key={key} found={val is not None}")
                    return val
                except Exception as exc:
                    if attempt == 0 and _is_expired_token_error(exc):
                        print(f"[storage] GET expired token, refreshing...")
                        continue
                    print(f"[storage] GET FAILED key={key}: {type(exc).__name__}: {exc}")
                    return None
    # local fallback
    path = _resolve_storage_path(storage_path)
    data = _load_all(path)
    value = data.get(key)
    return value if isinstance(value, dict) else None


def set_item(key: str, value: dict[str, Any], *, storage_path: Optional[str] = None) -> None:
    if _use_samsara_storage():
        for attempt in range(2):
            db = _get_samsara_db(force_refresh=(attempt > 0))
            if db is not None:
                try:
                    db.put_dict(key, value)
                    print(f"[storage] PUT OK key={key}")
                    return
                except Exception as exc:
                    if attempt == 0 and _is_expired_token_error(exc):
                        print(f"[storage] PUT expired token, refreshing...")
                        continue
                    print(f"[storage] PUT FAILED key={key}: {type(exc).__name__}: {exc}")
                    # fall through to local fallback
                    break
    # local fallback
    path = _resolve_storage_path(storage_path)
    try:
        data = _load_all(path)
        data[key] = value
        _save_all(path, data)
    except OSError as exc:
        print(f"[storage] local write FAILED path={path}: {exc}")


def delete_item(key: str, *, storage_path: Optional[str] = None) -> None:
    if _use_samsara_storage():
        for attempt in range(2):
            db = _get_samsara_db(force_refresh=(attempt > 0))
            if db is not None:
                try:
                    db.delete(key)
                    return
                except Exception as exc:
                    if attempt == 0 and _is_expired_token_error(exc):
                        continue
                    pass
    # local fallback
    path = _resolve_storage_path(storage_path)
    data = _load_all(path)
    if key in data:
        del data[key]
        try:
            _save_all(path, data)
        except OSError:
            pass


def list_keys(*, storage_path: Optional[str] = None) -> list[str]:
    """Return all stored keys."""
    if _use_samsara_storage():
        for attempt in range(2):
            db = _get_samsara_db(force_refresh=(attempt > 0))
            if db is not None:
                try:
                    return list(db.keys())
                except Exception as exc:
                    if attempt == 0 and _is_expired_token_error(exc):
                        print(f"[storage] KEYS expired token, refreshing...")
                        continue
                    print(f"[storage] KEYS FAILED: {type(exc).__name__}: {exc}")
                    return []
    # local fallback
    path = _resolve_storage_path(storage_path)
    data = _load_all(path)
    return list(data.keys())
