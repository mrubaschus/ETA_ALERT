import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# Local convenience: load eta-alert/.env if present.
# In Samsara Functions, secrets are injected as env vars; this is a no-op there.
try:
    from pathlib import Path

    from dotenv import load_dotenv

    # Load eta-alert/.env for local development convenience.
    # IMPORTANT: do NOT override already-set environment variables by default.
    # This prevents accidental local `.env` values from stomping on hosted
    # Secrets/config if a `.env` file is ever bundled/deployed.
    dotenv_override = os.getenv("DOTENV_OVERRIDE", "0").lower().strip() in (
        "1",
        "true",
        "yes",
        "y",
    )
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=dotenv_override)
except Exception:
    pass

import requests

from storage import get_item, set_item, STORAGE_ERROR


def _maybe_apply_samsara_function_secrets_to_env() -> None:
    """Best-effort: load Samsara Functions secrets and apply to os.environ.

    In Samsara Functions, secrets may not be injected as process env vars by default.
    The official examples load a JSON blob from AWS SSM using the platform-provided
    `SamsaraFunctionSecretsPath` and then optionally `apply_to_env`.

    We do the same here so deployments can rely on either:
    - traditional env vars (local dev)
    - the Functions secrets store (hosted)

    This function is intentionally silent on failure (no secret values are logged).
    """

    # If the platform isn't present, do nothing.
    secrets_path = os.environ.get("SamsaraFunctionSecretsPath")
    if not secrets_path:
        return

    # Avoid re-loading on every call.
    if os.environ.get("_ETA_ALERT_SECRETS_APPLIED") == "1":
        return

    try:
        import json

        import boto3  # type: ignore

        # In the hosted environment, assume the function execution role for SSM access.
        # This matches the pattern used in samsarahq/functions-examples.
        role_arn = os.environ.get("SamsaraFunctionExecRoleArn")
        session_name = os.environ.get("SamsaraFunctionName") or "eta-alert"

        if role_arn:
            sts = boto3.client("sts")
            res = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)
            creds = res.get("Credentials") or {}
            ssm = boto3.client(
                "ssm",
                aws_access_key_id=creds.get("AccessKeyId"),
                aws_secret_access_key=creds.get("SecretAccessKey"),
                aws_session_token=creds.get("SessionToken"),
            )
        else:
            # Fallback: use ambient credentials if role arn isn't provided.
            ssm = boto3.client("ssm")

        param = ssm.get_parameter(Name=secrets_path, WithDecryption=True)
        raw_value = ((param or {}).get("Parameter") or {}).get("Value")
        if not isinstance(raw_value, str) or raw_value in ("", "null"):
            os.environ["_ETA_ALERT_SECRETS_APPLIED"] = "1"
            return

        secrets = json.loads(raw_value)
        if not isinstance(secrets, dict):
            os.environ["_ETA_ALERT_SECRETS_APPLIED"] = "1"
            return

        # Apply secrets to env without overwriting existing env vars.
        for k, v in secrets.items():
            if not isinstance(k, str):
                continue
            if k in os.environ:
                continue
            if v is None:
                continue
            os.environ[k] = str(v)

        # Compatibility mapping (common examples use SAMSARA_API_TOKEN / NOTIFY_WEBHOOK).
        if "SAMSARA_TOKEN" not in os.environ:
            for candidate in ("SAMSARA_API_TOKEN", "SAMSARA_API_KEY", "SAMSARA_KEY"):
                if candidate in os.environ and os.environ.get(candidate):
                    os.environ["SAMSARA_TOKEN"] = os.environ[candidate]
                    break
        if "WEBHOOK_URL" not in os.environ:
            for candidate in ("NOTIFY_WEBHOOK", "WEBHOOK_URL"):
                if candidate in os.environ and os.environ.get(candidate):
                    os.environ["WEBHOOK_URL"] = os.environ[candidate]
                    break

        os.environ["_ETA_ALERT_SECRETS_APPLIED"] = "1"
    except Exception:
        # Never crash if secrets loading fails; caller will still error out with
        # a clear Missing required env var message.
        return


def _maybe_apply_context_secrets_to_env(context: Any) -> None:
    """Best-effort: apply secrets provided by the Functions runtime context.

    Several official examples access secrets via `context.get_secrets()`.
    If available, we use it as the highest-priority hosted secrets source.
    """

    if context is None:
        return
    # Avoid reapplying in the same process.
    if os.environ.get("_ETA_ALERT_CONTEXT_SECRETS_APPLIED") == "1":
        return

    getter = getattr(context, "get_secrets", None)
    if getter is None or not callable(getter):
        return

    try:
        secrets = getter()
        if not isinstance(secrets, dict):
            os.environ["_ETA_ALERT_CONTEXT_SECRETS_APPLIED"] = "1"
            return

        for k, v in secrets.items():
            if not isinstance(k, str):
                continue
            if k in os.environ:
                continue
            if v is None:
                continue
            os.environ[k] = str(v)

        # Compatibility mapping (examples often use SAMSARA_API_TOKEN / NOTIFY_WEBHOOK).
        if "SAMSARA_TOKEN" not in os.environ:
            for candidate in ("SAMSARA_API_TOKEN", "SAMSARA_API_KEY", "SAMSARA_KEY"):
                if candidate in os.environ and os.environ.get(candidate):
                    os.environ["SAMSARA_TOKEN"] = os.environ[candidate]
                    break
        if "WEBHOOK_URL" not in os.environ:
            for candidate in ("NOTIFY_WEBHOOK", "WEBHOOK_URL"):
                if candidate in os.environ and os.environ.get(candidate):
                    os.environ["WEBHOOK_URL"] = os.environ[candidate]
                    break

        os.environ["_ETA_ALERT_CONTEXT_SECRETS_APPLIED"] = "1"
    except Exception:
        return


SAMSARA_TOKEN = os.getenv("SAMSARA_TOKEN")

# Email
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "postmark").lower().strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "alerts@schusterco.com")
EMAIL_TO_OVERRIDE = os.getenv("EMAIL_TO_OVERRIDE", "").strip()
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "").strip()
POSTMARK_SERVER_TOKEN = os.getenv("EMAIL_API_KEY") or os.getenv("POSTMARK_SERVER_TOKEN")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

# Outlook (Microsoft Graph)
OUTLOOK_TENANT_ID = os.getenv("OUTLOOK_TENANT_ID", "").strip()
OUTLOOK_CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID", "").strip()
OUTLOOK_CLIENT_SECRET = os.getenv("OUTLOOK_CLIENT_SECRET", "").strip()
# Mailbox to send as (email address or user id). Requires Graph Mail.Send permission.
OUTLOOK_SENDER = os.getenv("OUTLOOK_SENDER", "").strip() or EMAIL_FROM

_GRAPH_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": 0}

# Notification mode
# - email: send customer email alerts
# - webhook: send a JSON webhook to your system
# - both: do both (email + webhook)
NOTIFY_MODE = os.getenv("NOTIFY_MODE", "email").lower().strip()  # email | webhook | both

# Webhook
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_METHOD = os.getenv("WEBHOOK_METHOD", "POST").upper().strip()
WEBHOOK_HEADERS_JSON = os.getenv("WEBHOOK_HEADERS_JSON", "").strip()  # optional JSON object

# Optional: emit a small “heartbeat” webhook each run so you can confirm the scheduler is working.
# Safety: require an explicit allow flag so heartbeats can't be enabled accidentally.
_WEBHOOK_HEARTBEAT_REQUESTED = os.getenv("WEBHOOK_HEARTBEAT", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
_WEBHOOK_HEARTBEAT_ALLOWED = os.getenv("WEBHOOK_HEARTBEAT_ALLOWED", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
WEBHOOK_HEARTBEAT = _WEBHOOK_HEARTBEAT_REQUESTED and _WEBHOOK_HEARTBEAT_ALLOWED

# Testing / safety
DRY_RUN = os.getenv("DRY_RUN", "0").lower().strip() in ("1", "true", "yes", "y")
USE_SAMPLE_DATA = os.getenv("USE_SAMPLE_DATA", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
SAMPLE_CUSTOMER_EMAIL = os.getenv("SAMPLE_CUSTOMER_EMAIL", "test@example.com").strip()

# Display timezone for logs/emails (does not affect scheduling/logic).
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "UTC").strip() or "UTC"

# Debug
DEBUG_DUMP_FIRST_STOP = os.getenv("DEBUG_DUMP_FIRST_STOP", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
DEBUG_ROUTES_PAGINATION = os.getenv("DEBUG_ROUTES_PAGINATION", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
DEBUG_DUMP_FIRST_AUDIT_ENTRY = os.getenv(
    "DEBUG_DUMP_FIRST_AUDIT_ENTRY", "0"
).lower().strip() in ("1", "true", "yes", "y")
DEBUG_DUMP_FIRST_AUDIT_ENTRY_WITH_ETA = os.getenv(
    "DEBUG_DUMP_FIRST_AUDIT_ENTRY_WITH_ETA", "0"
).lower().strip() in ("1", "true", "yes", "y")
DEBUG_TRACKING_LINKS = os.getenv("DEBUG_TRACKING_LINKS", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
DEBUG_EXIT_AFTER_DUMP = os.getenv("DEBUG_EXIT_AFTER_DUMP", "1").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)

# Printing / diagnostics
PRINT_MATCHES = os.getenv("PRINT_MATCHES", "0").lower().strip() in ("1", "true", "yes", "y")
PRINT_MATCHES_LIMIT = int(os.getenv("PRINT_MATCHES_LIMIT", "50"))
PRINT_MATCHES_SCOPE = os.getenv("PRINT_MATCHES_SCOPE", "notify").lower().strip()  # notify | filtered

# Optional alert guards (affect notifications only, not printing/tracking).
ALERT_ONLY_NEXT_UPCOMING_STOP = os.getenv("ALERT_ONLY_NEXT_UPCOMING_STOP", "1").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)
REQUIRE_STOP_EN_ROUTE_FOR_ALERTS = os.getenv("REQUIRE_STOP_EN_ROUTE_FOR_ALERTS", "1").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)

# Print a per-run sorted list of route ETAs (earliest upcoming stop ETA per route)
PRINT_ROUTE_ETAS = os.getenv("PRINT_ROUTE_ETAS", "0").lower().strip() in ("1", "true", "yes", "y")
PRINT_ROUTE_ETAS_LIMIT = int(os.getenv("PRINT_ROUTE_ETAS_LIMIT", "200"))  # 0 => no limit

# Optional tracking link generation for console output/webhooks.
# Use a template like: https://your-app/tracking?routeId={route_id}&stopId={stop_id}
TRACKING_URL_TEMPLATE = os.getenv("TRACKING_URL_TEMPLATE", "").strip()

# If the list endpoint omits live sharing link fields, optionally fetch each route's details
# (used only to derive a tracking URL for console/webhook output).
FETCH_ROUTE_DETAILS_FOR_TRACKING = os.getenv(
    "FETCH_ROUTE_DETAILS_FOR_TRACKING", "0"
).lower().strip() in ("1", "true", "yes", "y")

# Optional enrichment (when stops don't have an email field)
CUSTOMER_LOOKUP_URL_TEMPLATE = os.getenv(
    "CUSTOMER_LOOKUP_URL_TEMPLATE", ""
).strip()

# Routing/ETA tuning
TARGET_MINUTES = float(os.getenv("TARGET_MINUTES", "60"))
WINDOW_MINUTES = float(os.getenv("WINDOW_MINUTES", "5"))

# Trigger semantics
# - crossing: notify when ETA crosses from >TARGET_MINUTES to <=TARGET_MINUTES
# - window: notify when ETA is within [TARGET-WINDOW, TARGET+WINDOW)
TRIGGER_MODE = os.getenv("TRIGGER_MODE", "crossing").lower().strip()  # crossing | window
TRIGGER_REQUIRE_CROSSING = os.getenv("TRIGGER_REQUIRE_CROSSING", "0").lower().strip() in (
    "1",
    "true",
    "yes",
    "y",
)

# When TRIGGER_MODE=crossing and we have no prior history for a stop:
# - window (default): only alert if within [TARGET-WINDOW, TARGET+WINDOW)
# - below_target: alert if minutes_now <= TARGET
# - none: never alert without history
TRIGGER_NO_HISTORY_MODE = os.getenv("TRIGGER_NO_HISTORY_MODE", "window").lower().strip()

# Address filtering (case-insensitive substring match)
ADDRESS_NAME_CONTAINS_ANY = os.getenv("ADDRESS_NAME_CONTAINS_ANY", "").strip()
ADDRESS_NAME_EXCLUDES_ANY = os.getenv("ADDRESS_NAME_EXCLUDES_ANY", "").strip()

# Route-level inclusion: if ANY stop on a route matches these substrings, treat the whole route as eligible
# (i.e., bypass the stop allowlist for other stops on the same route).
ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY = os.getenv(
    "ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY", ""
).strip()

# Samsara API
SAMSARA_BASE_URL = os.getenv("SAMSARA_BASE_URL", "https://api.samsara.com")
# Keep the user's suggested endpoint as default; allow override if your tenant uses a different one.
SAMSARA_ROUTES_PATH = os.getenv("SAMSARA_ROUTES_PATH", "/fleet/routes")

# /fleet/routes requires startTime/endTime per docs.
ROUTES_START_TIME = os.getenv("ROUTES_START_TIME", "").strip()  # optional explicit override
ROUTES_END_TIME = os.getenv("ROUTES_END_TIME", "").strip()  # optional explicit override
# Default to a multi-day window to avoid missing long-haul routes.
# If you have routes that can span multiple days, increase these (at the cost of more data).
ROUTES_LOOKBACK_MINUTES = int(os.getenv("ROUTES_LOOKBACK_MINUTES", "10080"))  # 7 days
ROUTES_LOOKAHEAD_MINUTES = int(os.getenv("ROUTES_LOOKAHEAD_MINUTES", "10080"))  # 7 days
ROUTES_PAGE_SIZE = int(os.getenv("ROUTES_PAGE_SIZE", "512"))
ROUTES_MAX_PAGES_PER_RUN = int(os.getenv("ROUTES_MAX_PAGES_PER_RUN", "5"))
ROUTES_INCLUDE = os.getenv("ROUTES_INCLUDE", "").strip()  # comma-separated include values

# Data source: full route polling vs incremental audit logs
DATA_SOURCE = os.getenv("DATA_SOURCE", "routes").lower().strip()  # routes | audit_logs
SAMSARA_AUDIT_LOGS_PATH = os.getenv(
    "SAMSARA_AUDIT_LOGS_PATH", "/fleet/routes/audit-logs/feed"
)
AUDIT_LOGS_CURSOR_PARAM = os.getenv("AUDIT_LOGS_CURSOR_PARAM", "after").strip()
# Docs: audit-log feed supports expand=route (and pagination cursor). Keep names configurable.
AUDIT_LOGS_EXPAND_PARAM = os.getenv("AUDIT_LOGS_EXPAND_PARAM", "expand").strip()
AUDIT_LOGS_EXPAND_VALUE = os.getenv("AUDIT_LOGS_EXPAND_VALUE", "route").strip()
AUDIT_LOGS_MAX_PAGES_PER_RUN = int(os.getenv("AUDIT_LOGS_MAX_PAGES_PER_RUN", "10"))

_AUDIT_CURSOR_STORAGE_KEY = "__audit_logs_end_cursor__"

HEADERS = {
    "Authorization": f"Bearer {SAMSARA_TOKEN}" if SAMSARA_TOKEN else "",
    "Content-Type": "application/json",
}


def _reload_runtime_config_from_env() -> None:
    """Reload selected config from environment variables.

    Some hosted runtimes inject secrets/config late in the init lifecycle.
    This function ensures we don't permanently cache missing values at import time.
    """

    def _truthy(name: str, default: str = "0") -> bool:
        return os.getenv(name, default).lower().strip() in ("1", "true", "yes", "y")

    global SAMSARA_TOKEN
    global NOTIFY_MODE
    global WEBHOOK_URL, WEBHOOK_METHOD, WEBHOOK_HEADERS_JSON
    global _WEBHOOK_HEARTBEAT_REQUESTED, _WEBHOOK_HEARTBEAT_ALLOWED, WEBHOOK_HEARTBEAT
    global DRY_RUN, USE_SAMPLE_DATA
    global DISPLAY_TIMEZONE
    global TARGET_MINUTES, WINDOW_MINUTES
    global TRIGGER_MODE, TRIGGER_REQUIRE_CROSSING, TRIGGER_NO_HISTORY_MODE
    global ADDRESS_NAME_CONTAINS_ANY, ADDRESS_NAME_EXCLUDES_ANY
    global ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY
    global _ADDR_ALLOW_PATTERNS, _ADDR_DENY_PATTERNS, _ROUTE_FORCE_INCLUDE_PATTERNS
    global SAMSARA_BASE_URL, SAMSARA_ROUTES_PATH
    global ROUTES_START_TIME, ROUTES_END_TIME
    global ROUTES_LOOKBACK_MINUTES, ROUTES_LOOKAHEAD_MINUTES
    global ROUTES_PAGE_SIZE, ROUTES_MAX_PAGES_PER_RUN, ROUTES_INCLUDE
    global DATA_SOURCE
    global SAMSARA_AUDIT_LOGS_PATH, AUDIT_LOGS_CURSOR_PARAM
    global AUDIT_LOGS_EXPAND_PARAM, AUDIT_LOGS_EXPAND_VALUE
    global AUDIT_LOGS_MAX_PAGES_PER_RUN
    global HEADERS

    # Hosted: pull secrets into env if they aren't injected by default.
    _maybe_apply_samsara_function_secrets_to_env()

    # Core secrets/config
    SAMSARA_TOKEN = os.getenv("SAMSARA_TOKEN")

    # Notification mode
    NOTIFY_MODE = os.getenv("NOTIFY_MODE", "email").lower().strip()

    # Webhook
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
    WEBHOOK_METHOD = os.getenv("WEBHOOK_METHOD", "POST").upper().strip()
    WEBHOOK_HEADERS_JSON = os.getenv("WEBHOOK_HEADERS_JSON", "").strip()

    _WEBHOOK_HEARTBEAT_REQUESTED = _truthy("WEBHOOK_HEARTBEAT", "0")
    _WEBHOOK_HEARTBEAT_ALLOWED = _truthy("WEBHOOK_HEARTBEAT_ALLOWED", "0")
    WEBHOOK_HEARTBEAT = _WEBHOOK_HEARTBEAT_REQUESTED and _WEBHOOK_HEARTBEAT_ALLOWED

    # Testing / safety
    DRY_RUN = _truthy("DRY_RUN", "0")
    USE_SAMPLE_DATA = _truthy("USE_SAMPLE_DATA", "0")

    # Display
    DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "UTC").strip() or "UTC"

    # Routing/ETA tuning
    try:
        TARGET_MINUTES = float(os.getenv("TARGET_MINUTES", "60"))
    except Exception:
        TARGET_MINUTES = 60.0
    try:
        WINDOW_MINUTES = float(os.getenv("WINDOW_MINUTES", "5"))
    except Exception:
        WINDOW_MINUTES = 5.0

    # Trigger semantics
    TRIGGER_MODE = os.getenv("TRIGGER_MODE", "crossing").lower().strip()
    TRIGGER_REQUIRE_CROSSING = _truthy("TRIGGER_REQUIRE_CROSSING", "0")
    TRIGGER_NO_HISTORY_MODE = os.getenv("TRIGGER_NO_HISTORY_MODE", "window").lower().strip()

    # Address filtering
    ADDRESS_NAME_CONTAINS_ANY = os.getenv("ADDRESS_NAME_CONTAINS_ANY", "").strip()
    ADDRESS_NAME_EXCLUDES_ANY = os.getenv("ADDRESS_NAME_EXCLUDES_ANY", "").strip()
    ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY = os.getenv(
        "ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY", ""
    ).strip()

    _ADDR_ALLOW_PATTERNS = _split_csv_patterns(ADDRESS_NAME_CONTAINS_ANY)
    _ADDR_DENY_PATTERNS = _split_csv_patterns(ADDRESS_NAME_EXCLUDES_ANY)
    _ROUTE_FORCE_INCLUDE_PATTERNS = _split_csv_patterns(ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY)

    # Samsara API
    SAMSARA_BASE_URL = os.getenv("SAMSARA_BASE_URL", "https://api.samsara.com")
    SAMSARA_ROUTES_PATH = os.getenv("SAMSARA_ROUTES_PATH", "/fleet/routes")

    ROUTES_START_TIME = os.getenv("ROUTES_START_TIME", "").strip()
    ROUTES_END_TIME = os.getenv("ROUTES_END_TIME", "").strip()
    try:
        ROUTES_LOOKBACK_MINUTES = int(os.getenv("ROUTES_LOOKBACK_MINUTES", "10080"))
    except Exception:
        ROUTES_LOOKBACK_MINUTES = 10080
    try:
        ROUTES_LOOKAHEAD_MINUTES = int(os.getenv("ROUTES_LOOKAHEAD_MINUTES", "10080"))
    except Exception:
        ROUTES_LOOKAHEAD_MINUTES = 10080
    try:
        ROUTES_PAGE_SIZE = int(os.getenv("ROUTES_PAGE_SIZE", "512"))
    except Exception:
        ROUTES_PAGE_SIZE = 512
    try:
        ROUTES_MAX_PAGES_PER_RUN = int(os.getenv("ROUTES_MAX_PAGES_PER_RUN", "5"))
    except Exception:
        ROUTES_MAX_PAGES_PER_RUN = 5
    ROUTES_INCLUDE = os.getenv("ROUTES_INCLUDE", "").strip()

    # Data source
    DATA_SOURCE = os.getenv("DATA_SOURCE", "routes").lower().strip()
    SAMSARA_AUDIT_LOGS_PATH = os.getenv("SAMSARA_AUDIT_LOGS_PATH", "/fleet/routes/audit-logs/feed")
    AUDIT_LOGS_CURSOR_PARAM = os.getenv("AUDIT_LOGS_CURSOR_PARAM", "after").strip()
    AUDIT_LOGS_EXPAND_PARAM = os.getenv("AUDIT_LOGS_EXPAND_PARAM", "expand").strip()
    AUDIT_LOGS_EXPAND_VALUE = os.getenv("AUDIT_LOGS_EXPAND_VALUE", "route").strip()
    try:
        AUDIT_LOGS_MAX_PAGES_PER_RUN = int(os.getenv("AUDIT_LOGS_MAX_PAGES_PER_RUN", "10"))
    except Exception:
        AUDIT_LOGS_MAX_PAGES_PER_RUN = 10

    # Headers must be built from the *current* token.
    HEADERS = {
        "Authorization": f"Bearer {SAMSARA_TOKEN}" if SAMSARA_TOKEN else "",
        "Content-Type": "application/json",
    }


_ROUTE_DETAILS_CACHE: dict[str, dict[str, Any]] = {}
_ROUTE_DETAILS_STATS = {"attempted": 0, "ok": 0, "error": 0}


def _fetch_route_details(route_id: str) -> Optional[dict[str, Any]]:
    if not route_id:
        return None
    cached = _ROUTE_DETAILS_CACHE.get(route_id)
    if isinstance(cached, dict):
        return cached

    _require_env("SAMSARA_TOKEN", SAMSARA_TOKEN)

    base = SAMSARA_ROUTES_PATH.rstrip("/")
    url = f"{SAMSARA_BASE_URL}{base}/{route_id}"
    params: dict[str, Any] = {}
    if ROUTES_INCLUDE:
        params["include"] = ROUTES_INCLUDE

    _ROUTE_DETAILS_STATS["attempted"] += 1
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        # Common shapes: {data:{...}} or {route:{...}} or just {...}
        candidate = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(candidate, dict):
            route_obj = candidate
        elif isinstance(payload, dict) and isinstance(payload.get("route"), dict):
            route_obj = payload["route"]
        elif isinstance(payload, dict):
            route_obj = payload
        else:
            route_obj = {}

        if isinstance(route_obj, dict) and route_obj:
            _ROUTE_DETAILS_CACHE[route_id] = route_obj
        _ROUTE_DETAILS_STATS["ok"] += 1
        return route_obj if isinstance(route_obj, dict) else None
    except Exception:
        _ROUTE_DETAILS_STATS["error"] += 1
        return None


def _require_env(name: str, value: Optional[str]) -> str:
    if not value:
        raise RuntimeError(f"Missing required secret/env var: {name}")
    return value


def _safe_json_loads(value: str) -> Optional[dict[str, Any]]:
    if not value:
        return None
    try:
        import json

        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def minutes_until(iso_utc: str) -> float:
    # Samsara timestamps are typically RFC3339 like 2026-01-14T21:15:00Z
    t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return (t - datetime.now(timezone.utc)).total_seconds() / 60.0


_PHONE_RE = re.compile(
    r"(?:(?:\+?1[\s\-\.]*)?)"
    r"(?:\(\s*(\d{3})\s*\)|(\d{3}))[\s\-\.]*(\d{3})[\s\-\.]*(\d{4})"
    r"(?:\s*(?:x|ext\.?|extension)\s*(\d{1,6}))?",
    re.IGNORECASE,
)


def _extract_phone_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (phone_e164, extension) if a phone-like pattern is found."""
    if not isinstance(text, str) or not text.strip():
        return None, None

    m = _PHONE_RE.search(text)
    if not m:
        return None, None

    area = m.group(1) or m.group(2)
    prefix = m.group(3)
    line = m.group(4)
    ext = m.group(5)
    if not (area and prefix and line):
        return None, None

    digits10 = f"{area}{prefix}{line}"
    if len(digits10) != 10 or not digits10.isdigit():
        return None, None

    phone_e164 = f"+1{digits10}"
    ext_clean = ext.strip() if isinstance(ext, str) and ext.strip() else None
    return phone_e164, ext_clean


def _stop_notes_text(stop: dict[str, Any]) -> str:
    # Samsara payloads differ by endpoint/tenant; check a handful of likely fields.
    candidates: list[Any] = [
        stop.get("notes"),
        stop.get("note"),
        stop.get("stopNotes"),
        stop.get("instructions"),
        stop.get("customerNotes"),
    ]
    sul = stop.get("singleUseLocation")
    if isinstance(sul, dict):
        candidates.extend([
            sul.get("notes"),
            sul.get("note"),
            sul.get("instructions"),
        ])
    addr = stop.get("address")
    if isinstance(addr, dict):
        candidates.extend([
            addr.get("notes"),
            addr.get("note"),
        ])

    parts: list[str] = []
    for c in candidates:
        if isinstance(c, str) and c.strip():
            parts.append(c.strip())
    return "\n".join(parts)


def _parse_rfc3339(iso_utc: str) -> datetime:
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))


def _get_display_tzinfo() -> tuple[Any, str]:
    # Returns (tzinfo, label). Always falls back to UTC.
    if DISPLAY_TIMEZONE.upper() in ("UTC", "Z"):
        return timezone.utc, "UTC"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(DISPLAY_TIMEZONE)
        return tz, DISPLAY_TIMEZONE
    except Exception:
        return timezone.utc, "UTC"


def _format_dt_for_display(dt_utc: datetime) -> str:
    tz, _label = _get_display_tzinfo()
    local = dt_utc.astimezone(tz)
    # Windows strftime doesn't support %-I, so strip leading zero manually.
    s = local.strftime("%Y-%m-%d %I:%M %p %Z")
    return s.replace(" 0", " ")


def _select_best_live_sharing_url(links_any: Any) -> Optional[str]:
    if not isinstance(links_any, list) or not links_any:
        return None

    best_url: Optional[str] = None
    best_expiry: Optional[datetime] = None
    now = datetime.now(timezone.utc)
    for item in links_any:
        if not isinstance(item, dict):
            continue
        url = item.get("liveSharingUrl")
        if not (isinstance(url, str) and url.strip()):
            continue

        exp_raw = item.get("expiresAtTime")
        exp_dt: Optional[datetime] = None
        if isinstance(exp_raw, str) and exp_raw.strip():
            try:
                exp_dt = _parse_rfc3339(exp_raw)
            except Exception:
                exp_dt = None

        # Prefer any non-expired link. If multiple, choose the latest expiry.
        if exp_dt is None or exp_dt > now:
            if best_expiry is None:
                best_url = url.strip()
                best_expiry = exp_dt
            else:
                if exp_dt is not None and (best_expiry is None or exp_dt > best_expiry):
                    best_url = url.strip()
                    best_expiry = exp_dt

    return best_url


def _build_tracking_url(*, stop: dict[str, Any], route: dict[str, Any]) -> Optional[str]:
    # 1) Template override
    if TRACKING_URL_TEMPLATE:
        rid = route.get("id")
        sid = stop.get("id")
        try:
            return TRACKING_URL_TEMPLATE.format(route_id=rid, stop_id=sid)
        except Exception:
            # Fall through to best-effort extraction.
            pass

    # 2) Stop-level live-sharing URL
    stop_live = stop.get("liveSharingUrl")
    if isinstance(stop_live, str) and stop_live.strip():
        return stop_live.strip()

    # 3) Stop-level live-sharing link objects
    best = _select_best_live_sharing_url(stop.get("locationLiveSharingLinks"))
    if best:
        return best

    # 4) Route-level live-sharing link objects
    links_any = route.get("recurringRouteLiveSharingLinks")
    best = _select_best_live_sharing_url(links_any)
    if best:
        return best

    # 5) If still missing, optionally fetch route details and re-check route links.
    if FETCH_ROUTE_DETAILS_FOR_TRACKING:
        rid = route.get("id")
        if rid is not None:
            details = _fetch_route_details(str(rid))
            if isinstance(details, dict):
                best = _select_best_live_sharing_url(details.get("recurringRouteLiveSharingLinks"))
                if best:
                    return best

    # 6) Best-effort extraction from common fields
    candidates: list[Any] = [
        stop.get("trackingLink"),
        stop.get("trackingUrl"),
        stop.get("trackingURL"),
        stop.get("shareLink"),
        stop.get("shareUrl"),
        (stop.get("externalIds") or {}).get("trackingLink") if isinstance(stop.get("externalIds"), dict) else None,
        (route.get("externalIds") or {}).get("trackingLink") if isinstance(route.get("externalIds"), dict) else None,
        route.get("trackingLink"),
        route.get("trackingUrl"),
        route.get("trackingURL"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def within_target_window(minutes: float) -> bool:
    # Example: target=60, window=5 => [55, 65)
    lower = TARGET_MINUTES - WINDOW_MINUTES
    upper = TARGET_MINUTES + WINDOW_MINUTES
    return lower <= minutes < upper


def _split_csv_patterns(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip().lower() for p in value.split(",") if p.strip()]


_ADDR_ALLOW_PATTERNS = _split_csv_patterns(ADDRESS_NAME_CONTAINS_ANY)
_ADDR_DENY_PATTERNS = _split_csv_patterns(ADDRESS_NAME_EXCLUDES_ANY)
_ROUTE_FORCE_INCLUDE_PATTERNS = _split_csv_patterns(ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY)


def _address_is_suppressed(stop: dict[str, Any]) -> bool:
    # Suppression is applied only at notification time.
    if not _ADDR_DENY_PATTERNS:
        return False
    haystack = _stop_address_name(stop).lower()
    return any(p in haystack for p in _ADDR_DENY_PATTERNS)


def _stop_address_name(stop: dict[str, Any]) -> str:
    # Prefer structured address name, then singleUseLocation address, then stop name.
    addr = stop.get("address")
    if isinstance(addr, dict) and isinstance(addr.get("name"), str) and addr.get("name"):
        return addr["name"].strip()
    sul = stop.get("singleUseLocation")
    if isinstance(sul, dict) and isinstance(sul.get("address"), str) and sul.get("address"):
        return sul["address"].strip()
    name = stop.get("name")
    return name.strip() if isinstance(name, str) else ""


def _stop_is_completed(stop: dict[str, Any]) -> bool:
    # Treat a stop as completed if Samsara indicates an actual arrival/departure time,
    # or if the stop state is a completed terminal state.
    if isinstance(stop.get("actualDepartureTime"), str) and stop.get("actualDepartureTime").strip():
        return True
    if isinstance(stop.get("actualArrivalTime"), str) and stop.get("actualArrivalTime").strip():
        return True

    state = stop.get("state")
    if isinstance(state, str) and state.strip():
        s = state.strip().lower()
        if s in ("arrived", "departed", "completed", "complete", "done", "canceled", "cancelled", "skipped"):
            return True
    return False


def _address_filter_passes(stop: dict[str, Any], *, bypass_allowlist: bool = False) -> bool:
    # If allowlist is provided, require at least one match.
    # Note: denylist is handled separately as a notification suppression rule,
    # so excluded stops can still be listed/printed.
    if not _ADDR_ALLOW_PATTERNS and not _ADDR_DENY_PATTERNS:
        return True

    haystack = _stop_address_name(stop).lower()
    if bypass_allowlist:
        return True
    if _ADDR_ALLOW_PATTERNS and not any(p in haystack for p in _ADDR_ALLOW_PATTERNS):
        return False
    return True


def _route_force_include(route: dict[str, Any]) -> bool:
    if not _ROUTE_FORCE_INCLUDE_PATTERNS:
        return False
    stops = route.get("stops")
    if not isinstance(stops, list) or not stops:
        return False
    for s in stops:
        if not isinstance(s, dict):
            continue
        haystack = _stop_address_name(s).lower()
        if any(p in haystack for p in _ROUTE_FORCE_INCLUDE_PATTERNS):
            return True
    return False


def _should_notify_for_minutes(*, stop_state: Optional[dict[str, Any]], minutes_now: float) -> bool:
    # Only future ETAs.
    if minutes_now < 0:
        return False

    last_minutes: Optional[float] = None
    if isinstance(stop_state, dict):
        lm = stop_state.get("lastMinutes")
        try:
            if lm is not None:
                last_minutes = float(lm)
        except Exception:
            last_minutes = None

    if TRIGGER_MODE == "window":
        return within_target_window(minutes_now)

    # TRIGGER_MODE=crossing
    # If we have history: alert on downward crossing past target.
    if last_minutes is not None:
        return last_minutes > TARGET_MINUTES and minutes_now <= TARGET_MINUTES

    # No history yet.
    # If strict crossing is required, do NOT alert on first observation.
    if TRIGGER_REQUIRE_CROSSING:
        return False

    # Otherwise, use configured fallback behavior so we don't miss if we start tracking late.
    if TRIGGER_NO_HISTORY_MODE == "below_target":
        return minutes_now <= TARGET_MINUTES
    if TRIGGER_NO_HISTORY_MODE == "none":
        return False
    # Default: window
    return within_target_window(minutes_now)


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compute_routes_time_window() -> tuple[str, str]:
    if ROUTES_START_TIME and ROUTES_END_TIME:
        return ROUTES_START_TIME, ROUTES_END_TIME

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=max(0, ROUTES_LOOKBACK_MINUTES))
    end = now + timedelta(minutes=max(0, ROUTES_LOOKAHEAD_MINUTES))
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


def _generate_sample_routes() -> list[dict[str, Any]]:
    # Single route, single stop, ETA close to target window.
    eta = datetime.now(timezone.utc) + timedelta(minutes=max(0.0, TARGET_MINUTES - 1.0))
    return [
        {
            "id": "sample-route-1",
            "name": "Sample Route",
            "vehicle": {"id": "sample-vehicle-1", "name": "Sample Truck"},
            "stops": [
                {
                    "id": "sample-stop-1",
                    "name": "Sample Stop",
                    "eta": eta.isoformat().replace("+00:00", "Z"),
                    "customerEmail": SAMPLE_CUSTOMER_EMAIL,
                }
            ],
        }
    ]


def _bool_env(name: str) -> bool:
    return os.getenv(name, "0").lower().strip() in ("1", "true", "yes", "y")


def _select_debug_fields(obj: dict[str, Any], *, allow_keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in allow_keys:
        v = obj.get(k)
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            # Only include shallow identifiers to avoid leaking addresses/PII.
            out[k] = {"id": v.get("id"), "name": v.get("name")}
    return out


def _eta_related_kv(obj: dict[str, Any]) -> dict[str, Any]:
    keys = sorted([k for k in obj.keys() if isinstance(k, str)])
    interesting = [
        k
        for k in keys
        if any(token in k.lower() for token in ("eta", "arrival", "depart", "schedule", "enroute"))
    ]
    result: dict[str, Any] = {}
    for k in interesting:
        v = obj.get(k)
        # Avoid dumping big/nested objects.
        if isinstance(v, (str, int, float, bool)) or v is None:
            result[k] = v
    return result


def _ms_to_rfc3339_z(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _maybe_debug_dump(routes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not DEBUG_DUMP_FIRST_STOP:
        return None

    if not routes:
        dump = {"debug": "no routes returned"}
        print(dump)
        return dump

    first_route = routes[0] if isinstance(routes[0], dict) else {}
    stops = first_route.get("stops") if isinstance(first_route, dict) else None
    first_stop: dict[str, Any] = {}
    if isinstance(stops, list) and stops and isinstance(stops[0], dict):
        first_stop = stops[0]

    route_allow = ["id", "name", "scheduledRouteStartTime", "scheduledRouteEndTime", "actualRouteStartTime", "actualRouteEndTime"]
    stop_allow = ["id", "name", "state", "eta", "arrivalTime", "arrivalTimeUtc", "scheduledArrivalTime", "enRouteTime", "actualArrivalTime", "actualDepartureTime"]

    dump = {
        "debug": {
            "note": "Sanitized dump of first route/stop to find ETA fields",
            "routes_returned": len(routes),
            "first_route_keys": sorted(list(first_route.keys())),
            "first_stop_keys": sorted(list(first_stop.keys())) if first_stop else [],
            "first_route_selected": _select_debug_fields(first_route, allow_keys=route_allow),
            "first_route_eta_related": _eta_related_kv(first_route),
            "first_stop_selected": _select_debug_fields(first_stop, allow_keys=stop_allow) if first_stop else {},
            "first_stop_eta_related": _eta_related_kv(first_stop) if first_stop else {},
            "stops_count_first_route": len(stops) if isinstance(stops, list) else None,
        }
    }

    print(dump)
    return dump


def _maybe_debug_dump_audit_entry(entries: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not (DEBUG_DUMP_FIRST_AUDIT_ENTRY or DEBUG_DUMP_FIRST_AUDIT_ENTRY_WITH_ETA):
        return None

    if not entries:
        dump = {"debug_audit": "no audit entries returned"}
        print(dump)
        return dump

    def entry_has_eta(e: dict[str, Any]) -> bool:
        changes = e.get("changes") if isinstance(e.get("changes"), dict) else {}
        before = changes.get("before") if isinstance(changes.get("before"), dict) else {}
        after = changes.get("after") if isinstance(changes.get("after"), dict) else {}
        for bucket in (before, after):
            stops = bucket.get("stops")
            if isinstance(stops, list):
                for s in stops:
                    if isinstance(s, dict) and isinstance(s.get("eta"), str) and s.get("eta"):
                        return True
        return False

    first: dict[str, Any] = {}
    if DEBUG_DUMP_FIRST_AUDIT_ENTRY_WITH_ETA:
        for e in entries:
            if isinstance(e, dict) and entry_has_eta(e):
                first = e
                break
    if not first:
        first = entries[0] if isinstance(entries[0], dict) else {}

    route = first.get("route") if isinstance(first.get("route"), dict) else {}
    stop_ref = first.get("stop") if isinstance(first.get("stop"), dict) else {}
    event_details = first.get("eventDetails") if isinstance(first.get("eventDetails"), dict) else {}

    changes = first.get("changes") if isinstance(first.get("changes"), dict) else {}
    before = changes.get("before") if isinstance(changes.get("before"), dict) else {}
    after = changes.get("after") if isinstance(changes.get("after"), dict) else {}

    before_stops = before.get("stops") if isinstance(before.get("stops"), list) else []
    after_stops = after.get("stops") if isinstance(after.get("stops"), list) else []

    first_before_stop = (
        before_stops[0] if before_stops and isinstance(before_stops[0], dict) else {}
    )
    first_after_stop = after_stops[0] if after_stops and isinstance(after_stops[0], dict) else {}

    route_stops = route.get("stops") if isinstance(route.get("stops"), list) else []
    first_route_stop = route_stops[0] if route_stops and isinstance(route_stops[0], dict) else {}

    dump = {
        "debug_audit": {
            "note": "Sanitized dump of first audit entry to find ETA fields",
            "entry_keys": sorted(list(first.keys())),
            "type": first.get("type"),
            "operation": first.get("operation"),
            "time": first.get("time") or first.get("eventTime") or first.get("happenedAtTime"),
            "stop_ref": {"id": stop_ref.get("id")},
            "event_details_keys": sorted(list(event_details.keys())),
            "event_details_eta_related": _eta_related_kv(event_details),
            "route_selected": _select_debug_fields(
                route,
                allow_keys=[
                    "id",
                    "name",
                    "scheduledRouteStartTime",
                    "scheduledRouteEndTime",
                    "actualRouteStartTime",
                    "actualRouteEndTime",
                ],
            ),
            "route_eta_related": _eta_related_kv(route),
            "before_stop_keys": sorted(list(first_before_stop.keys())) if first_before_stop else [],
            "before_stop_eta_related": _eta_related_kv(first_before_stop) if first_before_stop else {},
            "after_stop_keys": sorted(list(first_after_stop.keys())) if first_after_stop else [],
            "after_stop_eta_related": _eta_related_kv(first_after_stop) if first_after_stop else {},
            "route_stop_keys": sorted(list(first_route_stop.keys())) if first_route_stop else [],
            "route_stop_eta_related": _eta_related_kv(first_route_stop) if first_route_stop else {},
        }
    }

    print(dump)
    return dump


def _postmark_send_email(*, to_email: str, subject: str, text_body: str, html_body: Optional[str]) -> None:
    token = _require_env("EMAIL_API_KEY (Postmark server token)", POSTMARK_SERVER_TOKEN)
    payload: dict[str, Any] = {
        "From": EMAIL_FROM,
        "To": to_email,
        "Subject": subject,
        "TextBody": text_body,
    }
    if html_body:
        payload["HtmlBody"] = html_body
    if EMAIL_REPLY_TO:
        payload["ReplyTo"] = EMAIL_REPLY_TO
    r = requests.post(
        "https://api.postmarkapp.com/email",
        headers={
            "X-Postmark-Server-Token": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


def _sendgrid_send_email(*, to_email: str, subject: str, text_body: str, html_body: Optional[str]) -> None:
    api_key = _require_env("SENDGRID_API_KEY", SENDGRID_API_KEY)
    content: list[dict[str, str]] = [{"type": "text/plain", "value": text_body}]
    if html_body:
        content.append({"type": "text/html", "value": html_body})

    personalization: dict[str, Any] = {"to": [{"email": to_email}]}
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [personalization],
            "from": {"email": EMAIL_FROM},
            **({"reply_to": {"email": EMAIL_REPLY_TO}} if EMAIL_REPLY_TO else {}),
            "subject": subject,
            "content": content,
        },
        timeout=20,
    )
    # SendGrid returns 202 Accepted on success.
    if r.status_code not in (200, 202):
        r.raise_for_status()


def _graph_get_access_token() -> str:
    import time

    _require_env("OUTLOOK_TENANT_ID", OUTLOOK_TENANT_ID)
    _require_env("OUTLOOK_CLIENT_ID", OUTLOOK_CLIENT_ID)
    _require_env("OUTLOOK_CLIENT_SECRET", OUTLOOK_CLIENT_SECRET)

    now = int(time.time())
    cached = _GRAPH_TOKEN_CACHE.get("access_token")
    expires_at = int(_GRAPH_TOKEN_CACHE.get("expires_at") or 0)
    if isinstance(cached, str) and cached and now < max(0, expires_at - 60):
        return cached

    token_url = f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}/oauth2/v2.0/token"
    r = requests.post(
        token_url,
        data={
            "client_id": OUTLOOK_CLIENT_ID,
            "client_secret": OUTLOOK_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    r.raise_for_status()

    data = r.json()
    if not isinstance(data, dict):
        data = {}
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Outlook token response missing access_token")
    expires_in = data.get("expires_in")
    try:
        expires_in_i = int(expires_in)
    except Exception:
        expires_in_i = 3000

    _GRAPH_TOKEN_CACHE["access_token"] = token
    _GRAPH_TOKEN_CACHE["expires_at"] = now + max(60, expires_in_i)
    return token


def _outlook_send_email(*, to_email: str, subject: str, text_body: str, html_body: Optional[str]) -> None:
    # Prefer HTML, but always ensure something is sent.
    content_type = "HTML" if html_body else "Text"
    content = html_body or text_body

    sender = _require_env("OUTLOOK_SENDER", OUTLOOK_SENDER)
    token = _graph_get_access_token()

    msg: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": content_type, "content": content},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if EMAIL_REPLY_TO:
        msg["replyTo"] = [{"emailAddress": {"address": EMAIL_REPLY_TO}}]

    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": msg, "saveToSentItems": True},
        timeout=30,
    )
    r.raise_for_status()


def _escape_html(value: Any) -> str:
    s = "" if value is None else str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _build_email_content(*, stop: dict[str, Any], eta_iso: str, route: Optional[dict[str, Any]]) -> tuple[str, str, str]:
    route_obj = route or {}
    stop_name = stop.get("name") or stop.get("id") or "(unknown stop)"
    route_name = route_obj.get("name") or route_obj.get("id") or "(unknown route)"
    tracking_url = _build_tracking_url(stop=stop, route=route_obj) or ""
    customer_name = _compute_customer_name(route_obj) if isinstance(route_obj, dict) else None

    try:
        minutes = minutes_until(eta_iso)
        minutes_s = f"{minutes:.0f}"
    except Exception:
        minutes_s = "(unknown)"

    customer_tag = f" ({customer_name})" if customer_name else ""
    subject_tpl = os.getenv(
        "EMAIL_SUBJECT",
        "Delivery ETA Alert{customer_tag}: {stop} (~{minutes} min)",
    )
    try:
        subject = subject_tpl.format(
            stop=stop_name,
            route=route_name,
            minutes=minutes_s,
            eta=eta_iso,
            customer=(customer_name or ""),
            customer_tag=customer_tag,
        )
    except Exception:
        subject = subject_tpl

    try:
        eta_display = _format_dt_for_display(_parse_rfc3339(eta_iso))
    except Exception:
        eta_display = eta_iso

    text = (
        "Delivery ETA Alert\n"
        f"Stop: {stop_name}\n"
        f"Route: {route_name}\n"
        + (f"Customer: {customer_name}\n" if customer_name else "")
        + f"ETA ({DISPLAY_TIMEZONE}): {eta_display}\n"
        + f"ETA (UTC): {eta_iso}\n"
        + f"Minutes until ETA: {minutes_s}\n"
        + (f"Tracking: {tracking_url}\n" if tracking_url else "")
    )

    stop_name_h = _escape_html(stop_name)
    route_name_h = _escape_html(route_name)
    customer_row_html = (
        f'<div style="font-size:13px;color:#6b7280;margin-top:4px;">Customer: {_escape_html(customer_name)}</div>'
        if customer_name
        else ""
    )
    eta_h = _escape_html(eta_display)
    eta_utc_h = _escape_html(eta_iso)
    minutes_h = _escape_html(minutes_s)
    tracking_h = _escape_html(tracking_url)

    button_html = ""
    if tracking_url:
        button_html = f"""
                <tr>
                    <td style=\"padding: 16px 24px 24px 24px;\">
                        <a href=\"{tracking_h}\" style=\"display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:600;\">Open Live Tracking</a>
                    </td>
                </tr>
                """

    html = f"""
        <!doctype html>
        <html>
            <body style=\"margin:0;padding:0;background:#f3f4f6;\">
                <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#f3f4f6;padding:24px 0;\">
                    <tr>
                        <td align=\"center\">
                            <table role=\"presentation\" width=\"600\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e5e7eb;\">
                                <tr>
                                    <td style=\"background:#111827;color:#ffffff;padding:18px 24px;font-family:Segoe UI, Arial, sans-serif;\">
                                        <div style=\"font-size:16px;font-weight:700;\">Delivery ETA Alert</div>
                                        <div style=\"font-size:13px;opacity:.85;margin-top:4px;\">A shipment is nearing its destination</div>
                                    </td>
                                </tr>
                                <tr>
                                    <td style=\"padding:18px 24px 0 24px;font-family:Segoe UI, Arial, sans-serif;color:#111827;\">
                                        <div style=\"font-size:18px;font-weight:700;\">{stop_name_h}</div>
                                        <div style=\"font-size:13px;color:#6b7280;margin-top:4px;\">Route: {route_name_h}</div>
                                        {customer_row_html}
                                    </td>
                                </tr>
                                <tr>
                                    <td style=\"padding:14px 24px 0 24px;font-family:Segoe UI, Arial, sans-serif;\">
                                        <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border:1px solid #e5e7eb;border-radius:12px;\">
                                            <tr>
                                                <td style=\"padding:14px 16px;color:#111827;font-size:14px;\">
                                                      <div><strong>ETA ({_escape_html(DISPLAY_TIMEZONE)}):</strong> {eta_h}</div>
                                                      <div style=\"margin-top:6px;color:#6b7280;font-size:12px;\"><strong>ETA (UTC):</strong> {eta_utc_h}</div>
                                                    <div style=\"margin-top:6px;\"><strong>Minutes until ETA:</strong> {minutes_h}</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                                {button_html}
                                <tr>
                                    <td style=\"padding:0 24px 18px 24px;font-family:Segoe UI, Arial, sans-serif;color:#6b7280;font-size:12px;\">
                                        This is an automated notification. Times are shown in { _escape_html(DISPLAY_TIMEZONE) } (UTC also included).
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                </table>
            </body>
        </html>
        """

    return subject, text, html


def send_email(
    *,
    to_email: str,
    stop: dict[str, Any],
    eta_iso: str,
    route: Optional[dict[str, Any]] = None,
) -> None:
    subject, text_body, html_body = _build_email_content(stop=stop, eta_iso=eta_iso, route=route)

    if DRY_RUN:
        print(
            f"[DRY_RUN] Would send email via {EMAIL_PROVIDER} to={to_email} stop={stop.get('id')} eta={eta_iso}"
        )
        return

    if EMAIL_PROVIDER == "sendgrid":
        _sendgrid_send_email(to_email=to_email, subject=subject, text_body=text_body, html_body=html_body)
        return

    if EMAIL_PROVIDER == "outlook":
        _outlook_send_email(to_email=to_email, subject=subject, text_body=text_body, html_body=html_body)
        return

    # Default to Postmark
    _postmark_send_email(to_email=to_email, subject=subject, text_body=text_body, html_body=html_body)


def send_webhook(*, stop: dict[str, Any], eta_iso: str, route: Optional[dict[str, Any]] = None) -> None:
    url = _require_env("WEBHOOK_URL", WEBHOOK_URL)
    method = WEBHOOK_METHOD or "POST"
    if method not in ("POST", "PUT"):
        raise RuntimeError(f"Unsupported WEBHOOK_METHOD: {method}")

    route_obj = route or {}
    tracking_url = _build_tracking_url(stop=stop, route=route_obj) if isinstance(route_obj, dict) else None

    customer_name = _compute_customer_name(route_obj) if isinstance(route_obj, dict) else None

    next_stop_id: Optional[str] = None
    next_stop_eta: Optional[str] = None
    next_stop_minutes: Optional[int] = None
    if isinstance(route_obj, dict):
        next_stop_id, next_stop_eta, next_stop_minutes = _route_next_upcoming_stop_info(route_obj)

    minutes_until_eta: Optional[float] = None
    try:
        minutes_until_eta = minutes_until(eta_iso)
    except Exception:
        minutes_until_eta = None

    minutes_until_rounded = int(round(minutes_until_eta)) if minutes_until_eta is not None else None

    notes_text = _stop_notes_text(stop)
    phone_e164, phone_ext = _extract_phone_from_text(notes_text)

    payload = {
        "type": "eta_alert",
        "customerName": customer_name,
        "targetMinutes": TARGET_MINUTES,
        "windowMinutes": WINDOW_MINUTES,
        "minutesUntil": minutes_until_rounded,
        "minutes": minutes_until_rounded,
        "stop": {
            "id": stop.get("id"),
            "name": stop.get("name"),
            "externalIds": stop.get("externalIds"),
            "state": stop.get("state"),
            "phone": phone_e164,
            "phoneExtension": phone_ext,
            "isNextStop": (str(stop.get("id")) == next_stop_id) if stop.get("id") is not None else False,
        },
        "route": {
            "id": route_obj.get("id"),
            "name": route_obj.get("name"),
            "externalIds": route_obj.get("externalIds"),
            "customerName": customer_name,
            "nextStopId": next_stop_id,
            "nextStopEta": next_stop_eta,
            "nextStopMinutesUntil": next_stop_minutes,
        },
        "vehicle": route_obj.get("vehicle"),
        "driver": route_obj.get("driver"),
        "trackingUrl": tracking_url,
        "eta": eta_iso,
        "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    headers = {"Content-Type": "application/json"}
    extra_headers = _safe_json_loads(WEBHOOK_HEADERS_JSON) or {}
    for k, v in extra_headers.items():
        if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
            headers[k] = str(v)

    if DRY_RUN:
        print(f"[DRY_RUN] Would send webhook {method} url={url} payload_type=eta_alert")
        return

    print(
        f"[webhook] sending stopId={stop.get('id')} minutesUntil={minutes_until_rounded} customer={customer_name} phone={phone_e164} eta={eta_iso}"
    )

    if method == "PUT":
        r = requests.put(url, headers=headers, json=payload, timeout=20)
    else:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()


def send_webhook_heartbeat(*, summary: dict[str, Any]) -> None:
    url = _require_env("WEBHOOK_URL", WEBHOOK_URL)
    method = WEBHOOK_METHOD or "POST"
    if method not in ("POST", "PUT"):
        raise RuntimeError(f"Unsupported WEBHOOK_METHOD: {method}")

    payload = {
        "type": "eta_alert_heartbeat",
        "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": summary,
    }

    headers = {"Content-Type": "application/json"}
    extra_headers = _safe_json_loads(WEBHOOK_HEADERS_JSON) or {}
    for k, v in extra_headers.items():
        if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
            headers[k] = str(v)

    if DRY_RUN:
        print(f"[DRY_RUN] Would send webhook {method} url={url} payload_type=eta_alert_heartbeat")
        return

    if method == "PUT":
        r = requests.put(url, headers=headers, json=payload, timeout=20)
    else:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()


def lookup_customer_email(stop_id: str) -> Optional[str]:
    if not CUSTOMER_LOOKUP_URL_TEMPLATE:
        return None

    url = CUSTOMER_LOOKUP_URL_TEMPLATE.format(stop_id=stop_id)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    # Flexible parsing: allow {"email": "x"} or nested structures
    if isinstance(data, dict):
        if isinstance(data.get("email"), str):
            return data["email"].strip()
        if isinstance(data.get("customerEmail"), str):
            return data["customerEmail"].strip()

    return None


def fetch_routes() -> list[dict[str, Any]]:
    if USE_SAMPLE_DATA:
        return _generate_sample_routes()

    _require_env("SAMSARA_TOKEN", SAMSARA_TOKEN)

    start_time, end_time = _compute_routes_time_window()
    url = f"{SAMSARA_BASE_URL}{SAMSARA_ROUTES_PATH}"

    all_routes: list[dict[str, Any]] = []
    cursor: Optional[str] = None

    for page_idx in range(1, max(1, ROUTES_MAX_PAGES_PER_RUN) + 1):
        params: dict[str, Any] = {
            "startTime": start_time,
            "endTime": end_time,
        }
        if ROUTES_PAGE_SIZE > 0:
            params["limit"] = min(max(1, ROUTES_PAGE_SIZE), 512)
        if cursor:
            params["after"] = cursor
        if ROUTES_INCLUDE:
            params["include"] = ROUTES_INCLUDE

        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        data = payload.get("data")
        if isinstance(data, list):
            all_routes.extend([x for x in data if isinstance(x, dict)])
        else:
            # Some endpoints return {routes:[...]}
            routes = payload.get("routes")
            if isinstance(routes, list):
                all_routes.extend([x for x in routes if isinstance(x, dict)])

        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            if DEBUG_ROUTES_PAGINATION:
                print(
                    {
                        "debug_routes_pagination": {
                            "page": page_idx,
                            "returned": len(all_routes),
                            "note": "No pagination object in response; stopping",
                        }
                    }
                )
            break

        cursor = pagination.get("endCursor") if isinstance(pagination.get("endCursor"), str) else None
        has_next = bool(pagination.get("hasNextPage"))

        if DEBUG_ROUTES_PAGINATION:
            print(
                {
                    "debug_routes_pagination": {
                        "page": page_idx,
                        "page_size": params.get("limit"),
                        "routes_so_far": len(all_routes),
                        "hasNextPage": has_next,
                        "endCursor_present": bool(cursor),
                    }
                }
            )
        if not has_next or not cursor:
            break

    return all_routes


def _fetch_audit_logs_page(*, cursor: Optional[str]) -> tuple[list[dict[str, Any]], Optional[str], bool]:
    _require_env("SAMSARA_TOKEN", SAMSARA_TOKEN)

    url = f"{SAMSARA_BASE_URL}{SAMSARA_AUDIT_LOGS_PATH}"
    params: dict[str, Any] = {}
    if cursor:
        params[AUDIT_LOGS_CURSOR_PARAM] = cursor
    if AUDIT_LOGS_EXPAND_VALUE:
        params[AUDIT_LOGS_EXPAND_PARAM] = AUDIT_LOGS_EXPAND_VALUE

    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()

    entries = payload.get("data")
    if not isinstance(entries, list):
        entries = []

    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        pagination = {}

    end_cursor = pagination.get("endCursor")
    has_next = bool(pagination.get("hasNextPage"))
    return entries, end_cursor if isinstance(end_cursor, str) else None, has_next


def _extract_audit_entry_route_and_stops(
    entry: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    route = entry.get("route")
    if not isinstance(route, dict):
        route = {}

    # Pull stop deltas from changes.before/after.
    changes = entry.get("changes")
    before_stops: list[dict[str, Any]] = []
    after_stops: list[dict[str, Any]] = []
    if isinstance(changes, dict):
        before = changes.get("before")
        after = changes.get("after")

        if isinstance(before, dict) and isinstance(before.get("stops"), list):
            before_stops = [s for s in before["stops"] if isinstance(s, dict)]

        if isinstance(after, dict) and isinstance(after.get("stops"), list):
            after_stops = [s for s in after["stops"] if isinstance(s, dict)]

    # Build a map of any ETA values present in before/after stop snapshots.
    eta_by_stop_id: dict[str, str] = {}
    for s in before_stops + after_stops:
        sid = s.get("id")
        eta = s.get("eta")
        if sid is not None and isinstance(eta, str) and eta:
            eta_by_stop_id[str(sid)] = eta

    # Optional: Some tenants may include eventDetails.stopEtaUpdated.etaMs.
    stop_ref = entry.get("stop")
    stop_id_from_entry: Optional[str] = None
    if isinstance(stop_ref, dict) and stop_ref.get("id") is not None:
        stop_id_from_entry = str(stop_ref.get("id"))

    event_details = entry.get("eventDetails")
    if isinstance(event_details, dict):
        stop_eta_updated = event_details.get("stopEtaUpdated")
        if isinstance(stop_eta_updated, dict) and stop_id_from_entry:
            eta_ms = stop_eta_updated.get("etaMs")
            try:
                if isinstance(eta_ms, str) and eta_ms.strip():
                    eta_by_stop_id[stop_id_from_entry] = _ms_to_rfc3339_z(int(eta_ms))
                elif isinstance(eta_ms, (int, float)):
                    eta_by_stop_id[stop_id_from_entry] = _ms_to_rfc3339_z(int(eta_ms))
            except Exception:
                pass

    # Choose which stop list to process:
    # - Prefer AFTER stops (the changed stop snapshots)
    # - Else BEFORE stops
    # - Else full route.stops (when expanded)
    stops_any: Any = None
    if after_stops:
        stops_any = after_stops
    elif before_stops:
        stops_any = before_stops
    else:
        stops_any = route.get("stops")

    normalized: list[dict[str, Any]] = []
    if isinstance(stops_any, list):
        for s in stops_any:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            sid_str = str(sid) if sid is not None else None
            if sid_str and sid_str in eta_by_stop_id and not _get_eta_iso(s):
                s = dict(s)
                s["eta"] = eta_by_stop_id[sid_str]
            normalized.append(s)

    # If we have ETA(s) but no stop objects, synthesize minimal stops.
    if not normalized and eta_by_stop_id:
        for sid_str, eta in eta_by_stop_id.items():
            normalized.append({"id": sid_str, "eta": eta})

    return route, normalized


def _get_eta_iso(stop: dict[str, Any]) -> Optional[str]:
    eta_iso = (
        stop.get("eta")
        or stop.get("arrivalTime")
        or stop.get("arrivalTimeUtc")
        or stop.get("scheduledArrivalTime")
    )
    return eta_iso if isinstance(eta_iso, str) and eta_iso else None


def _route_next_upcoming_stop_info(route: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (stop_id, eta_iso, minutes_until_int) for the earliest upcoming stop on a route."""
    stops = route.get("stops")
    if not isinstance(stops, list) or not stops:
        return None, None, None

    best_dt: Optional[datetime] = None
    best_eta: Optional[str] = None
    best_stop_id: Optional[str] = None

    for stop in stops:
        if not isinstance(stop, dict):
            continue
        if _stop_is_completed(stop):
            continue
        eta_iso = _get_eta_iso(stop)
        if not isinstance(eta_iso, str) or not eta_iso.strip():
            continue
        try:
            mins = minutes_until(eta_iso)
        except Exception:
            continue
        if mins < 0:
            continue
        try:
            dt = _parse_rfc3339(eta_iso)
        except Exception:
            continue
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best_eta = eta_iso
            sid = stop.get("id")
            best_stop_id = str(sid) if sid is not None else None

    if best_stop_id and best_eta and best_dt is not None:
        try:
            best_minutes = int(round(minutes_until(best_eta)))
        except Exception:
            best_minutes = None
        return best_stop_id, best_eta, best_minutes

    return None, None, None


def _compute_customer_name(route: dict[str, Any]) -> Optional[str]:
    """Infer a friendly customer name for a route.

    Rules:
    - If it's a Gerkin load (any stop contains 'gerkin'), customer is 'Gerkin'.
    - Else if any stop contains 'bomgaars', customer is 'Bomgaars'.
    """
    stops = route.get("stops")
    if not isinstance(stops, list) or not stops:
        return None

    for stop in stops:
        if not isinstance(stop, dict):
            continue
        if "gerkin" in _stop_address_name(stop).lower():
            return "Gerkin"

    for stop in stops:
        if not isinstance(stop, dict):
            continue
        if "bomgaars" in _stop_address_name(stop).lower():
            return "Bomgaars"

    return None


def main(event=None, context=None):
    # Hosted: some runtimes provide secrets via the invocation context.
    _maybe_apply_context_secrets_to_env(context)

    _reload_runtime_config_from_env()

    # Optional diagnostics to debug missing secrets in hosted runtimes.
    # Never prints secret values.
    if os.getenv("DEBUG_SECRETS", "0").lower().strip() in ("1", "true", "yes", "y"):
        print(
            {
                "secrets_debug": {
                    "has_SamsaraFunctionSecretsPath": bool(os.environ.get("SamsaraFunctionSecretsPath")),
                    "has_SamsaraFunctionExecRoleArn": bool(os.environ.get("SamsaraFunctionExecRoleArn")),
                    "has_SamsaraFunctionName": bool(os.environ.get("SamsaraFunctionName")),
                    "context_has_get_secrets": bool(getattr(context, "get_secrets", None)),
                    "context_secrets_applied": os.environ.get("_ETA_ALERT_CONTEXT_SECRETS_APPLIED") == "1",
                    "ssm_secrets_applied": os.environ.get("_ETA_ALERT_SECRETS_APPLIED") == "1",
                    "env_has_SAMSARA_TOKEN": bool(os.environ.get("SAMSARA_TOKEN")),
                    "env_has_SAMSARA_API_TOKEN": bool(os.environ.get("SAMSARA_API_TOKEN")),
                    "env_has_WEBHOOK_URL": bool(os.environ.get("WEBHOOK_URL")),
                    "env_has_NOTIFY_WEBHOOK": bool(os.environ.get("NOTIFY_WEBHOOK")),
                }
            }
        )
    # DATA_SOURCE=routs: poll all routes and scan stops
    # DATA_SOURCE=audit_logs: pull incremental changes from audit log feed using stored cursor
    routes: list[dict[str, Any]] = []
    audit_pages = 0
    audit_entries = 0
    audit_cursor_start: Optional[str] = None
    audit_cursor_end: Optional[str] = None

    if USE_SAMPLE_DATA:
        routes = _generate_sample_routes()
    elif DATA_SOURCE == "audit_logs":
        cursor_item = get_item(_AUDIT_CURSOR_STORAGE_KEY) or {}
        if isinstance(cursor_item.get("cursor"), str):
            audit_cursor_start = cursor_item["cursor"]

        cursor = audit_cursor_start
        for _ in range(max(0, AUDIT_LOGS_MAX_PAGES_PER_RUN)):
            entries, end_cursor, has_next = _fetch_audit_logs_page(cursor=cursor)
            audit_pages += 1
            audit_entries += len(entries)

            audit_debug_dump = _maybe_debug_dump_audit_entry(entries)
            if audit_debug_dump is not None and DEBUG_EXIT_AFTER_DUMP:
                return audit_debug_dump

            # Process page entries; only advance cursor if processing succeeds.
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                route, stops = _extract_audit_entry_route_and_stops(entry)
                if stops:
                    # Store as a pseudo-route so the existing processing loop can reuse logic.
                    routes.append({"_route": route, "stops": stops})

            if end_cursor:
                set_item(
                    _AUDIT_CURSOR_STORAGE_KEY,
                    {"cursor": end_cursor, "updatedAt": datetime.now(timezone.utc).isoformat()},
                )
                cursor = end_cursor
                audit_cursor_end = end_cursor

            if not has_next or not end_cursor:
                break
    else:
        routes = fetch_routes()

    debug_dump = _maybe_debug_dump(routes)
    if debug_dump is not None and DEBUG_EXIT_AFTER_DUMP:
        return debug_dump

    sent = 0
    would_send = 0
    skipped_already = 0
    skipped_no_email = 0
    skipped_no_eta = 0
    skipped_not_in_window = 0
    skipped_filtered = 0
    skipped_past = 0
    skipped_suppressed = 0
    skipped_not_next_stop = 0
    skipped_not_en_route = 0
    matches: list[dict[str, Any]] = []

    for route in routes:
        # In audit_logs mode we wrap the real route in _route.
        real_route = route.get("_route") if isinstance(route, dict) else None
        if not isinstance(real_route, dict):
            real_route = route if isinstance(route, dict) else {}

        # If a route contains a configured “seed” stop (e.g., a pickup at GERKIN WINDOWS),
        # bypass the stop allowlist for all stops on that route.
        route_bypass_allowlist = _route_force_include(route if isinstance(route, dict) else {})

        next_stop_id: Optional[str] = None
        next_stop_eta: Optional[str] = None
        next_stop_minutes: Optional[int] = None
        if isinstance(route, dict):
            next_stop_id, next_stop_eta, next_stop_minutes = _route_next_upcoming_stop_info(route)

        stops = (route.get("stops") if isinstance(route, dict) else None) or []
        if not isinstance(stops, list):
            continue

        for stop in stops:
            if not isinstance(stop, dict):
                continue

            stop_id = stop.get("id")
            if not stop_id:
                continue

            # Filter early so PRINT_MATCHES_SCOPE=filtered can show rows even when ETA is missing.
            if not _address_filter_passes(stop, bypass_allowlist=route_bypass_allowlist):
                skipped_filtered += 1
                continue

            # Ignore already-completed stops (their live-sharing links typically show arrived/departed).
            if _stop_is_completed(stop):
                skipped_past += 1
                continue

            # Different Samsara APIs expose ETA fields differently.
            # Common variants we've seen:
            # - stop.eta (ISO)
            # - stop.arrivalTime / stop.arrivalTimeUtc (ISO)
            # - stop.scheduledArrivalTime (ISO; less ideal but usable)
            eta_iso = _get_eta_iso(stop)
            minutes: Optional[float] = None
            if eta_iso:
                minutes = minutes_until(eta_iso)
                # Hard skip any past ETA to avoid reporting or notifying on stale routes/stops.
                if minutes < 0:
                    skipped_past += 1
                    continue
            if PRINT_MATCHES and PRINT_MATCHES_SCOPE == "filtered" and len(matches) < max(
                0, PRINT_MATCHES_LIMIT
            ):
                matches.append(
                    {
                        "routeId": real_route.get("id"),
                        "routeName": real_route.get("name"),
                        "stopId": stop.get("id"),
                        "stopName": stop.get("name"),
                        "addressName": _stop_address_name(stop),
                        "eta": eta_iso,
                        "minutesUntil": int(round(minutes)) if minutes is not None else None,
                        "wouldNotify": None,
                        "reason": "no_eta" if not eta_iso else "has_eta",
                    }
                )

            if not eta_iso:
                skipped_no_eta += 1
                continue

            # minutes is already computed above and guaranteed >= 0 here.
            if minutes is None:
                skipped_no_eta += 1
                continue

            stop_key = str(stop_id)
            state_raw = get_item(stop_key)
            storage_failed = state_raw is STORAGE_ERROR
            state = {} if (state_raw is None or storage_failed) else state_raw
            if isinstance(state, dict) and state.get("notified") is True:
                skipped_already += 1
                continue

            should_notify = _should_notify_for_minutes(stop_state=state, minutes_now=minutes)
            if not should_notify:
                skipped_not_in_window += 1
                # Still persist latest seen values.
                set_item(
                    stop_key,
                    {
                        **(state if isinstance(state, dict) else {}),
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso,
                        "lastMinutes": minutes,
                        "notified": False,
                    },
                )
                continue

            # Optional: suppress alerts until Samsara indicates the stop is en-route.
            if REQUIRE_STOP_EN_ROUTE_FOR_ALERTS:
                en_route_time = stop.get("enRouteTime")
                if not (isinstance(en_route_time, str) and en_route_time.strip()):
                    skipped_not_en_route += 1
                    set_item(
                        stop_key,
                        {
                            **(state if isinstance(state, dict) else {}),
                            "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                            "lastEta": eta_iso,
                            "lastMinutes": minutes,
                            "notified": False,
                            "notEnRoute": True,
                        },
                    )
                    continue

            # Optional: only alert for the next upcoming stop on the route.
            if ALERT_ONLY_NEXT_UPCOMING_STOP and next_stop_id and str(stop_id) != str(next_stop_id):
                skipped_not_next_stop += 1
                set_item(
                    stop_key,
                    {
                        **(state if isinstance(state, dict) else {}),
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso,
                        "lastMinutes": minutes,
                        "notified": False,
                        "notNextStop": True,
                        "nextStopId": next_stop_id,
                        "nextStopEta": next_stop_eta,
                        "nextStopMinutesUntil": next_stop_minutes,
                    },
                )
                continue

            # Suppress notifications for configured excluded stop names, but keep tracking/printing.
            if _address_is_suppressed(stop):
                skipped_suppressed += 1
                set_item(
                    stop_key,
                    {
                        **(state if isinstance(state, dict) else {}),
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso,
                        "lastMinutes": minutes,
                        "notified": False,
                        "suppressed": True,
                    },
                )
                continue

            if PRINT_MATCHES and PRINT_MATCHES_SCOPE == "notify" and len(matches) < max(
                0, PRINT_MATCHES_LIMIT
            ):
                matches.append(
                    {
                        "routeId": real_route.get("id"),
                        "routeName": real_route.get("name"),
                        "stopId": stop.get("id"),
                        "stopName": stop.get("name"),
                        "addressName": _stop_address_name(stop),
                        "eta": eta_iso,
                        "minutesUntil": int(round(minutes)),
                        "wouldNotify": True,
                        "reason": "notify_now",
                    }
                )

            # Notify via email/webhook based on NOTIFY_MODE
            if DRY_RUN:
                would_send += 1
                # Still persist latest seen values so crossing-mode has history,
                # but do not mark as notified during a dry run.
                set_item(
                    stop_key,
                    {
                        **(state if isinstance(state, dict) else {}),
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso,
                        "lastMinutes": minutes,
                        "notified": False,
                        "dryRunWouldSendAt": datetime.now(timezone.utc).isoformat(),
                        "targetMinutes": TARGET_MINUTES,
                        "windowMinutes": WINDOW_MINUTES,
                        "triggerMode": TRIGGER_MODE,
                    },
                )
                continue

            webhook_sent = False
            webhook_error: Optional[str] = None
            email_sent = False
            email_error: Optional[str] = None

            if NOTIFY_MODE in ("webhook", "both"):
                try:
                    send_webhook(stop=stop, eta_iso=eta_iso, route=real_route)
                    webhook_sent = True
                except Exception as e:
                    webhook_error = f"{type(e).__name__}: {e}"
                    print(f"[webhook] failed for stopId={stop_id}: {webhook_error}")

            if NOTIFY_MODE in ("email", "both"):
                # Prefer an explicit override (useful for testing / single-recipient mode).
                # Otherwise prefer an email on the stop, else optionally enrich.
                email = EMAIL_TO_OVERRIDE or (
                    stop.get("customerEmail")
                    or stop.get("email")
                    or stop.get("customer", {}).get("email")
                )
                if isinstance(email, str):
                    email = email.strip()

                # Only attempt enrichment when we're not forcing a single recipient.
                if not email and not EMAIL_TO_OVERRIDE:
                    email = lookup_customer_email(str(stop_id))

                if not email:
                    skipped_no_email += 1
                    # If webhook was sent, treat this stop as handled so we don't spam.
                    set_item(
                        stop_key,
                        {
                            **(state if isinstance(state, dict) else {}),
                            "notified": True if webhook_sent else False,
                            "sentAt": datetime.now(timezone.utc).isoformat() if webhook_sent else None,
                            "eta": eta_iso,
                            "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                            "lastEta": eta_iso,
                            "lastMinutes": minutes,
                            "targetMinutes": TARGET_MINUTES,
                            "windowMinutes": WINDOW_MINUTES,
                            "triggerMode": TRIGGER_MODE,
                            "webhookSentAt": datetime.now(timezone.utc).isoformat() if webhook_sent else None,
                            "emailSentAt": None,
                            "emailMissing": True,
                        },
                    )
                    if webhook_sent:
                        sent += 1
                    continue

                try:
                    send_email(to_email=email, stop=stop, eta_iso=eta_iso, route=real_route)
                    email_sent = True
                except Exception as e:
                    email_error = f"{type(e).__name__}: {e}"
                    print(f"[email] failed for stopId={stop_id}: {email_error}")

            sent_any = webhook_sent or email_sent
            if not sent_any:
                # Don't mark as notified if nothing actually sent.
                set_item(
                    stop_key,
                    {
                        **(state if isinstance(state, dict) else {}),
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso,
                        "lastMinutes": minutes,
                        "notified": False,
                        "targetMinutes": TARGET_MINUTES,
                        "windowMinutes": WINDOW_MINUTES,
                        "triggerMode": TRIGGER_MODE,
                        "webhookSentAt": None,
                        "emailSentAt": None,
                        "webhookError": webhook_error,
                        "emailError": email_error,
                        "emailMissing": False,
                    },
                )
                continue

            set_item(
                stop_key,
                {
                    **(state if isinstance(state, dict) else {}),
                    "notified": True,
                    "sentAt": datetime.now(timezone.utc).isoformat(),
                    "eta": eta_iso,
                    "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                    "lastEta": eta_iso,
                    "lastMinutes": minutes,
                    "targetMinutes": TARGET_MINUTES,
                    "windowMinutes": WINDOW_MINUTES,
                    "triggerMode": TRIGGER_MODE,
                    "webhookSentAt": datetime.now(timezone.utc).isoformat() if webhook_sent else None,
                    "emailSentAt": datetime.now(timezone.utc).isoformat() if email_sent else None,
                    "webhookError": webhook_error,
                    "emailError": email_error,
                    "emailMissing": False,
                },
            )
            sent += 1

    if PRINT_MATCHES:
        print(
            f"PRINT_MATCHES scope={PRINT_MATCHES_SCOPE} count={len(matches)} limit={PRINT_MATCHES_LIMIT}"
        )
        for row in matches:
            route_name = row.get("routeName") or row.get("routeId") or "(unknown route)"
            address_name = row.get("addressName") or row.get("stopName") or "(unknown stop)"
            eta = row.get("eta")
            eta_disp = eta
            if isinstance(eta, str) and eta.strip():
                try:
                    eta_disp = _format_dt_for_display(_parse_rfc3339(eta))
                except Exception:
                    eta_disp = eta
            mins = row.get("minutesUntil")
            reason = row.get("reason")
            print(f"- {route_name} | {address_name} | eta={eta_disp} | minutes={mins} | {reason}")

    if PRINT_ROUTE_ETAS and DATA_SOURCE == "routes":
        # For each route, find the earliest upcoming stop ETA (minutes >= 0), then sort.
        per_route: list[tuple[datetime, dict[str, Any]]] = []
        routes_with_any_eta = 0
        routes_with_live_sharing_field = 0
        routes_with_live_sharing_url = 0
        routes_with_tracking_url = 0
        routes_with_stop_live_sharing_url = 0
        routes_with_stop_location_links = 0
        for route in routes:
            if not isinstance(route, dict):
                continue

            route_bypass_allowlist = _route_force_include(route)

            links_any = route.get("recurringRouteLiveSharingLinks")
            if isinstance(links_any, list) and links_any:
                routes_with_live_sharing_field += 1
                if any(
                    isinstance(item, dict)
                    and isinstance(item.get("liveSharingUrl"), str)
                    and item.get("liveSharingUrl").strip()
                    for item in links_any
                ):
                    routes_with_live_sharing_url += 1

            stops_any = route.get("stops")
            if not isinstance(stops_any, list):
                continue

            best_eta: Optional[str] = None
            best_dt: Optional[datetime] = None
            best_stop: Optional[dict[str, Any]] = None
            for stop in stops_any:
                if not isinstance(stop, dict):
                    continue
                if not _address_filter_passes(stop, bypass_allowlist=route_bypass_allowlist):
                    continue
                if _stop_is_completed(stop):
                    continue
                eta_iso = _get_eta_iso(stop)
                if not eta_iso:
                    continue
                try:
                    mins = minutes_until(eta_iso)
                except Exception:
                    continue
                if mins < 0:
                    continue
                try:
                    dt = _parse_rfc3339(eta_iso)
                except Exception:
                    continue
                if best_dt is None or dt < best_dt:
                    best_dt = dt
                    best_eta = eta_iso
                    best_stop = stop

            if best_dt is not None and best_eta is not None:
                routes_with_any_eta += 1
                stop_obj = best_stop if isinstance(best_stop, dict) else {}
                if isinstance(stop_obj.get("liveSharingUrl"), str) and stop_obj.get("liveSharingUrl").strip():
                    routes_with_stop_live_sharing_url += 1
                if isinstance(stop_obj.get("locationLiveSharingLinks"), list) and stop_obj.get("locationLiveSharingLinks"):
                    routes_with_stop_location_links += 1
                per_route.append(
                    (
                        best_dt,
                        {
                            "routeId": route.get("id"),
                            "routeName": route.get("name"),
                            "stopId": stop_obj.get("id"),
                            "stopName": stop_obj.get("name"),
                            "earliestEta": best_eta,
                            "minutesUntil": int(round(minutes_until(best_eta))),
                            # Keep references so we can compute tracking only for printed rows.
                            "_routeObj": route,
                            "_stopObj": stop_obj,
                        },
                    )
                )

        per_route.sort(key=lambda t: t[0])
        limit = PRINT_ROUTE_ETAS_LIMIT
        rows = per_route if limit == 0 else per_route[: max(0, limit)]
        print(
            f"PRINT_ROUTE_ETAS routes_with_eta={routes_with_any_eta} shown={len(rows)} limit={PRINT_ROUTE_ETAS_LIMIT} total_routes_processed={len(routes)}"
        )
        if DEBUG_TRACKING_LINKS:
            print(
                "PRINT_ROUTE_ETAS tracking_summary "
                f"routes_with_live_sharing_field={routes_with_live_sharing_field} "
                f"routes_with_live_sharing_url={routes_with_live_sharing_url} "
                f"routes_with_stop_live_sharing_url={routes_with_stop_live_sharing_url} "
                f"routes_with_stop_location_links={routes_with_stop_location_links} "
                f"routes_with_tracking_url={routes_with_tracking_url} "
                f"routes_include={ROUTES_INCLUDE or '(none)'} "
                f"route_details_fetch_attempted={_ROUTE_DETAILS_STATS['attempted']} "
                f"route_details_fetch_ok={_ROUTE_DETAILS_STATS['ok']} "
                f"route_details_fetch_error={_ROUTE_DETAILS_STATS['error']}"
            )
        for _, row in rows:
            rn = row.get("routeName") or row.get("routeId") or "(unknown route)"
            sn = row.get("stopName") or row.get("stopId") or "(unknown stop)"
            sid = row.get("stopId") or ""
            route_obj = row.get("_routeObj") if isinstance(row.get("_routeObj"), dict) else {}
            stop_obj = row.get("_stopObj") if isinstance(row.get("_stopObj"), dict) else {}
            tu = _build_tracking_url(stop=stop_obj, route=route_obj) or ""
            if isinstance(tu, str) and tu.strip():
                routes_with_tracking_url += 1
            eta_iso = row.get("earliestEta")
            eta_disp = eta_iso
            if isinstance(eta_iso, str) and eta_iso.strip():
                try:
                    eta_disp = _format_dt_for_display(_parse_rfc3339(eta_iso))
                except Exception:
                    eta_disp = eta_iso
            print(
                f"- {rn} | stop={sn} | stopId={sid} | eta={eta_disp} | minutes={row.get('minutesUntil')} | track={tu}"
            )

    result = {
        "status": "ok",
        "routes": len(routes),
        "sent": sent,
        "would_send": would_send,
        "matches": matches,
        "skipped": {
            "already_notified": skipped_already,
            "no_email": skipped_no_email,
            "no_eta": skipped_no_eta,
            "not_in_window": skipped_not_in_window,
            "filtered": skipped_filtered,
            "suppressed": skipped_suppressed,
            "past": skipped_past,
            "not_next_stop": skipped_not_next_stop,
            "not_en_route": skipped_not_en_route,
        },
        "config": {
            "target_minutes": TARGET_MINUTES,
            "window_minutes": WINDOW_MINUTES,
            "email_provider": EMAIL_PROVIDER,
            "routes_path": SAMSARA_ROUTES_PATH,
            "data_source": DATA_SOURCE,
            "audit_logs_path": SAMSARA_AUDIT_LOGS_PATH,
            "routes_window": {
                "startTime": ROUTES_START_TIME or "computed",
                "endTime": ROUTES_END_TIME or "computed",
                "lookbackMinutes": ROUTES_LOOKBACK_MINUTES,
                "lookaheadMinutes": ROUTES_LOOKAHEAD_MINUTES,
            },
            "dry_run": DRY_RUN,
            "use_sample_data": USE_SAMPLE_DATA,
            "notify_mode": NOTIFY_MODE,
            "trigger_mode": TRIGGER_MODE,
            "trigger_require_crossing": TRIGGER_REQUIRE_CROSSING,
            "address_name_contains_any": ADDRESS_NAME_CONTAINS_ANY,
            "address_name_excludes_any": ADDRESS_NAME_EXCLUDES_ANY,
            "print_matches": PRINT_MATCHES,
            "print_matches_limit": PRINT_MATCHES_LIMIT,
            "print_matches_scope": PRINT_MATCHES_SCOPE,
            "print_route_etas": PRINT_ROUTE_ETAS,
            "print_route_etas_limit": PRINT_ROUTE_ETAS_LIMIT,
            "alert_only_next_upcoming_stop": ALERT_ONLY_NEXT_UPCOMING_STOP,
            "require_stop_en_route_for_alerts": REQUIRE_STOP_EN_ROUTE_FOR_ALERTS,
            "webhook_heartbeat": WEBHOOK_HEARTBEAT,
            "display_timezone": DISPLAY_TIMEZONE,
        },
        "audit_logs": {
            "pages": audit_pages,
            "entries": audit_entries,
            "cursor_start": audit_cursor_start,
            "cursor_end": audit_cursor_end,
        },
    }

    if WEBHOOK_HEARTBEAT and NOTIFY_MODE in ("webhook", "both"):
        try:
            send_webhook_heartbeat(summary={"sent": sent, "routes": len(routes), "data_source": DATA_SOURCE})
        except Exception as e:
            # Heartbeat should never take down the run.
            print(f"[heartbeat] failed: {type(e).__name__}: {e}")

    return result


if __name__ == "__main__":
    import argparse
    import time
    import traceback

    def _truthy(name: str) -> bool:
        return os.getenv(name, "0").lower().strip() in ("1", "true", "yes", "y")

    parser = argparse.ArgumentParser(description="ETA Alert runner")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously every --interval seconds (or RUN_FOREVER=1).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("LOOP_SECONDS", "300")),
        help="Loop interval in seconds (default: 300).",
    )
    args = parser.parse_args()

    run_forever = args.loop or _truthy("RUN_FOREVER")
    interval = max(5, int(args.interval))

    if not run_forever:
        print(main())
    else:
        while True:
            print(f"--- {datetime.now(timezone.utc).isoformat()}Z ---")
            try:
                print(main())
            except Exception:
                traceback.print_exc()
            time.sleep(interval)
