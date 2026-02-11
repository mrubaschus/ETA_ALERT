"""ETA Alert — Simplified webhook-only alerting for Samsara routes.

Polls /fleet/routes, computes minutes-until-ETA for each stop, and fires
a webhook when a stop crosses the ~60-minute threshold.

Only 5 environment variables / secrets are needed:
  SAMSARA_TOKEN                                  — Samsara API bearer token
  WEBHOOK_URL                                    — destination for POST payloads
  ADDRESS_NAME_CONTAINS_ANY                      — comma-separated allowlist substrings
  ADDRESS_NAME_EXCLUDES_ANY                      — comma-separated denylist substrings
  ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY — route-level force-include substrings

Everything else is hardcoded.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Local .env convenience (no-op in Samsara Functions)
# ---------------------------------------------------------------------------
try:
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(
        dotenv_path=Path(__file__).with_name(".env"),
        override=os.getenv("DOTENV_OVERRIDE", "0").lower().strip() in ("1", "true", "yes"),
    )
except Exception:
    pass

import requests
from storage import get_item, set_item, delete_item, list_keys, STORAGE_ERROR

# ── Hardcoded configuration ──────────────────────────────────────────────────
TARGET_MINUTES: float = 60.0
WINDOW_MINUTES: float = 5.0
DISPLAY_TIMEZONE: str = "America/Chicago"
TRIGGER_MODE: str = "crossing"            # crossing | window
TRIGGER_NO_HISTORY_MODE: str = "below_target"   # fallback when no prior observation
REQUIRE_STOP_EN_ROUTE: bool = True
ALERT_ONLY_NEXT_STOP: bool = True
COLD_START_DEDUP_MINUTES: float = 10.0  # 2× polling interval; skip if en-route longer than this with no stored state
STORAGE_TTL_HOURS: float = 24.0          # purge stored stop entries older than this

SAMSARA_BASE_URL: str = "https://api.samsara.com"
SAMSARA_ROUTES_PATH: str = "/fleet/routes"
ROUTES_LOOKBACK_MINUTES: int = 10080      # 7 days
ROUTES_LOOKAHEAD_MINUTES: int = 10080     # 7 days
ROUTES_PAGE_SIZE: int = 50                # API caps at 50/page regardless
ROUTES_MAX_PAGES: int = 0                 # 0 = unlimited (fetch all pages)


# ── Secrets / env loading ────────────────────────────────────────────────────

def _maybe_apply_samsara_function_secrets_to_env() -> None:
    """Load secrets from AWS SSM in Samsara Functions hosted runtime."""
    secrets_path = os.environ.get("SamsaraFunctionSecretsPath")
    if not secrets_path:
        return
    if os.environ.get("_ETA_ALERT_SECRETS_APPLIED") == "1":
        return
    try:
        import json
        import boto3  # type: ignore

        role_arn = os.environ.get("SamsaraFunctionExecRoleArn")
        session_name = os.environ.get("SamsaraFunctionName") or "eta-alert"

        if role_arn:
            sts = boto3.client("sts")
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name).get("Credentials") or {}
            ssm = boto3.client(
                "ssm",
                aws_access_key_id=creds.get("AccessKeyId"),
                aws_secret_access_key=creds.get("SecretAccessKey"),
                aws_session_token=creds.get("SessionToken"),
            )
        else:
            ssm = boto3.client("ssm")

        raw = ((ssm.get_parameter(Name=secrets_path, WithDecryption=True) or {}).get("Parameter") or {}).get("Value")
        if not isinstance(raw, str) or raw in ("", "null"):
            os.environ["_ETA_ALERT_SECRETS_APPLIED"] = "1"
            return

        secrets = json.loads(raw)
        if not isinstance(secrets, dict):
            os.environ["_ETA_ALERT_SECRETS_APPLIED"] = "1"
            return

        for k, v in secrets.items():
            if isinstance(k, str) and k not in os.environ and v is not None:
                os.environ[k] = str(v)

        # Compatibility aliases
        if "SAMSARA_TOKEN" not in os.environ:
            for c in ("SAMSARA_API_TOKEN", "SAMSARA_API_KEY", "SAMSARA_KEY"):
                if os.environ.get(c):
                    os.environ["SAMSARA_TOKEN"] = os.environ[c]
                    break
        if "WEBHOOK_URL" not in os.environ:
            for c in ("NOTIFY_WEBHOOK",):
                if os.environ.get(c):
                    os.environ["WEBHOOK_URL"] = os.environ[c]
                    break

        os.environ["_ETA_ALERT_SECRETS_APPLIED"] = "1"
    except Exception as exc:
        print(f"[WARN] SSM secrets loading failed: {type(exc).__name__}: {exc}")


def _maybe_apply_context_secrets(context: Any) -> None:
    """Load secrets from the Samsara Functions runtime context object."""
    if context is None:
        return
    if os.environ.get("_ETA_ALERT_CTX_SECRETS") == "1":
        return
    getter = getattr(context, "get_secrets", None)
    if not callable(getter):
        return
    try:
        secrets = getter()
        if not isinstance(secrets, dict):
            os.environ["_ETA_ALERT_CTX_SECRETS"] = "1"
            return
        for k, v in secrets.items():
            if isinstance(k, str) and k not in os.environ and v is not None:
                os.environ[k] = str(v)
        if "SAMSARA_TOKEN" not in os.environ:
            for c in ("SAMSARA_API_TOKEN", "SAMSARA_API_KEY", "SAMSARA_KEY"):
                if os.environ.get(c):
                    os.environ["SAMSARA_TOKEN"] = os.environ[c]
                    break
        if "WEBHOOK_URL" not in os.environ:
            for c in ("NOTIFY_WEBHOOK",):
                if os.environ.get(c):
                    os.environ["WEBHOOK_URL"] = os.environ[c]
                    break
        os.environ["_ETA_ALERT_CTX_SECRETS"] = "1"
    except Exception:
        pass


def _load_env() -> dict[str, Any]:
    """Read the 5 configurable env vars and return a runtime config dict."""
    _maybe_apply_samsara_function_secrets_to_env()
    token = os.getenv("SAMSARA_TOKEN", "").strip()
    webhook = os.getenv("WEBHOOK_URL", "").strip()
    allow = os.getenv("ADDRESS_NAME_CONTAINS_ANY", "").strip()
    deny = os.getenv("ADDRESS_NAME_EXCLUDES_ANY", "").strip()
    force = os.getenv("ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY", "").strip()
    return {
        "token": token,
        "webhook_url": webhook,
        "allow": _csv(allow),
        "deny": _csv(deny),
        "force_include": _csv(force),
        "headers": {
            "Authorization": f"Bearer {token}" if token else "",
            "Content-Type": "application/json",
        },
        # Raw strings for the result payload
        "ADDRESS_NAME_CONTAINS_ANY": allow,
        "ADDRESS_NAME_EXCLUDES_ANY": deny,
        "ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY": force,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _csv(value: str) -> list[str]:
    """Split comma-separated string into lowered, stripped, non-empty parts.

    Patterns starting with '=' are kept as-is (exact match marker).
    All patterns are lowered for case-insensitive comparison.
    """
    return [p.strip().lower() for p in value.split(",") if p.strip()] if value else []


def _pattern_matches(haystack: str, pattern: str) -> bool:
    """Check if a pattern matches a haystack string (both already lowered).

    - '=bomgaars supply #2' -> exact match (address must equal 'bomgaars supply #2')
    - 'bomgaars' -> substring match (address must contain 'bomgaars')
    """
    if pattern.startswith("="):
        return haystack == pattern[1:]
    return pattern in haystack


def _require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _parse_rfc3339(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def minutes_until(iso_utc: str) -> float:
    return (_parse_rfc3339(iso_utc) - datetime.now(timezone.utc)).total_seconds() / 60.0


def within_target_window(minutes: float) -> bool:
    return (TARGET_MINUTES - WINDOW_MINUTES) <= minutes < (TARGET_MINUTES + WINDOW_MINUTES)


def _display_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(DISPLAY_TIMEZONE), DISPLAY_TIMEZONE
    except Exception:
        return timezone.utc, "UTC"


def _format_dt(dt_utc: datetime) -> str:
    tz, _ = _display_tz()
    s = dt_utc.astimezone(tz).strftime("%Y-%m-%d %I:%M %p %Z")
    return s.replace(" 0", " ")


# ── Stop / route helpers ────────────────────────────────────────────────────

def _stop_address_name(stop: dict[str, Any]) -> str:
    addr = stop.get("address")
    if isinstance(addr, dict) and isinstance(addr.get("name"), str) and addr["name"]:
        return addr["name"].strip()
    sul = stop.get("singleUseLocation")
    if isinstance(sul, dict) and isinstance(sul.get("address"), str) and sul["address"]:
        return sul["address"].strip()
    name = stop.get("name")
    return name.strip() if isinstance(name, str) else ""


def _stop_is_completed(stop: dict[str, Any]) -> bool:
    if isinstance(stop.get("actualDepartureTime"), str) and stop["actualDepartureTime"].strip():
        return True
    if isinstance(stop.get("actualArrivalTime"), str) and stop["actualArrivalTime"].strip():
        return True
    state = stop.get("state")
    if isinstance(state, str):
        s = state.strip().lower()
        if s in ("arrived", "departed", "completed", "complete", "done", "canceled", "cancelled", "skipped"):
            return True
    return False


def _get_eta_iso(stop: dict[str, Any]) -> Optional[str]:
    v = stop.get("eta") or stop.get("arrivalTime") or stop.get("arrivalTimeUtc") or stop.get("scheduledArrivalTime")
    return v if isinstance(v, str) and v else None


def _address_filter_passes(stop: dict[str, Any], *, allow: list[str], bypass: bool = False) -> bool:
    if not allow:
        return True
    if bypass:
        return True
    h = _stop_address_name(stop).lower()
    return any(_pattern_matches(h, p) for p in allow)


def _address_is_suppressed(stop: dict[str, Any], *, deny: list[str]) -> bool:
    if not deny:
        return False
    h = _stop_address_name(stop).lower()
    return any(_pattern_matches(h, p) for p in deny)


def _route_force_include(route: dict[str, Any], *, patterns: list[str]) -> bool:
    if not patterns:
        return False
    stops = route.get("stops")
    if not isinstance(stops, list):
        return False
    for s in stops:
        if not isinstance(s, dict):
            continue
        if any(_pattern_matches(_stop_address_name(s).lower(), p) for p in patterns):
            return True
    return False


def _route_next_upcoming_stop(route: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (stop_id, eta_iso, minutes_rounded) for the earliest upcoming stop."""
    stops = route.get("stops")
    if not isinstance(stops, list):
        return None, None, None
    best_dt: Optional[datetime] = None
    best_eta: Optional[str] = None
    best_id: Optional[str] = None
    for s in stops:
        if not isinstance(s, dict) or _stop_is_completed(s):
            continue
        eta = _get_eta_iso(s)
        if not eta:
            continue
        try:
            m = minutes_until(eta)
        except Exception:
            continue
        if m < 0:
            continue
        dt = _parse_rfc3339(eta)
        if best_dt is None or dt < best_dt:
            best_dt, best_eta, best_id = dt, eta, str(s.get("id")) if s.get("id") is not None else None
    if best_id and best_eta:
        try:
            return best_id, best_eta, int(round(minutes_until(best_eta)))
        except Exception:
            return best_id, best_eta, None
    return None, None, None


def _should_notify(*, stop_state: Optional[dict[str, Any]], minutes_now: float) -> bool:
    if minutes_now < 0:
        return False
    last: Optional[float] = None
    if isinstance(stop_state, dict):
        # If previous attempt had webhook error, ignore lastMinutes so retry uses fresh logic
        if stop_state.get("webhookError") and stop_state.get("notified") is False:
            last = None
        else:
            lm = stop_state.get("lastMinutes")
            try:
                if lm is not None:
                    last = float(lm)
            except Exception:
                pass
    if TRIGGER_MODE == "window":
        return within_target_window(minutes_now)
    # crossing mode
    if last is not None:
        return last > TARGET_MINUTES and minutes_now <= TARGET_MINUTES
    # No history — use below_target fallback
    if TRIGGER_NO_HISTORY_MODE == "below_target":
        return minutes_now <= TARGET_MINUTES
    if TRIGGER_NO_HISTORY_MODE == "none":
        return False
    return within_target_window(minutes_now)


def _en_route_minutes(stop: dict[str, Any]) -> Optional[float]:
    """How many minutes ago did this stop become en-route?"""
    ert = stop.get("enRouteTime")
    if not isinstance(ert, str) or not ert.strip():
        return None
    try:
        en_route_dt = _parse_rfc3339(ert)
        return (datetime.now(timezone.utc) - en_route_dt).total_seconds() / 60.0
    except Exception:
        return None


def _compute_customer_name(route: dict[str, Any]) -> Optional[str]:
    """Derive customer name from stop addresses on the route."""
    stops = route.get("stops")
    if not isinstance(stops, list):
        return None
    for s in stops:
        if isinstance(s, dict) and "gerkin" in _stop_address_name(s).lower():
            return "Gerkin"
    for s in stops:
        if isinstance(s, dict) and "bomgaars" in _stop_address_name(s).lower():
            return "Bomgaars"
    return None


# ── Phone extraction ─────────────────────────────────────────────────────────

# 10-digit (with optional area code parens) or 7-digit phone numbers
_PHONE_RE = re.compile(
    r"(?:(?:\+?1[\s\-\.]*)?)"
    r"(?:\(\s*(\d{3})\s*\)|(\d{3}))[\s\-\.]*(\d{3})[\s\-\.]*(\d{4})"
    r"(?:\s*(?:x|ext\.?|extension)\s*(\d{1,6}))?",
    re.IGNORECASE,
)
_PHONE_7_RE = re.compile(
    r"(\d{3})[\s\-\.]+(\d{4})"
    r"(?:\s*(?:x|ext\.?|extension)\s*(\d{1,6}))?",
    re.IGNORECASE,
)


def _extract_phone(text: str) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(text, str) or not text.strip():
        return None, None
    # Try 10-digit first
    m = _PHONE_RE.search(text)
    if m:
        area = m.group(1) or m.group(2)
        prefix, line, ext = m.group(3), m.group(4), m.group(5)
        if area and prefix and line:
            digits = f"{area}{prefix}{line}"
            if len(digits) == 10 and digits.isdigit():
                return f"1{digits}", (ext.strip() if ext and ext.strip() else None)
    # Fall back to 7-digit
    m7 = _PHONE_7_RE.search(text)
    if m7:
        prefix, line, ext = m7.group(1), m7.group(2), m7.group(3)
        if prefix and line:
            digits = f"{prefix}{line}"
            if len(digits) == 7 and digits.isdigit():
                return digits, (ext.strip() if ext and ext.strip() else None)
    return None, None


def _stop_notes_text(stop: dict[str, Any]) -> str:
    candidates: list[Any] = [
        stop.get("notes"), stop.get("note"), stop.get("stopNotes"),
        stop.get("instructions"), stop.get("customerNotes"),
    ]
    sul = stop.get("singleUseLocation")
    if isinstance(sul, dict):
        candidates += [sul.get("notes"), sul.get("note"), sul.get("instructions")]
    addr = stop.get("address")
    if isinstance(addr, dict):
        candidates += [addr.get("notes"), addr.get("note")]
    return "\n".join(c.strip() for c in candidates if isinstance(c, str) and c.strip())


# ── Tracking URL ─────────────────────────────────────────────────────────────

def _select_best_live_url(links: Any) -> Optional[str]:
    if not isinstance(links, list):
        return None
    now = datetime.now(timezone.utc)
    best_url: Optional[str] = None
    best_exp: Optional[datetime] = None
    for item in links:
        if not isinstance(item, dict):
            continue
        url = item.get("liveSharingUrl")
        if not isinstance(url, str) or not url.strip():
            continue
        exp_raw = item.get("expiresAtTime")
        exp: Optional[datetime] = None
        if isinstance(exp_raw, str) and exp_raw.strip():
            try:
                exp = _parse_rfc3339(exp_raw)
            except Exception:
                pass
        if exp is None or exp > now:
            if best_exp is None or (exp is not None and exp > best_exp):
                best_url, best_exp = url.strip(), exp
    return best_url


def _build_tracking_url(*, stop: dict[str, Any], route: dict[str, Any]) -> Optional[str]:
    # Stop-level live sharing
    v = stop.get("liveSharingUrl")
    if isinstance(v, str) and v.strip():
        return v.strip()
    best = _select_best_live_url(stop.get("locationLiveSharingLinks"))
    if best:
        return best
    # Route-level live sharing
    best = _select_best_live_url(route.get("recurringRouteLiveSharingLinks"))
    if best:
        return best
    # Fallback fields
    for obj in (stop, route):
        for key in ("trackingLink", "trackingUrl", "trackingURL", "shareLink", "shareUrl"):
            c = obj.get(key)
            if isinstance(c, str) and c.strip():
                return c.strip()
    return None


# ── Trailer extraction ───────────────────────────────────────────────────────

def _extract_trailer(route: dict[str, Any]) -> Optional[dict[str, Any]]:
    trailer = route.get("trailer")
    if isinstance(trailer, dict) and trailer.get("id"):
        return {"id": trailer.get("id"), "name": trailer.get("name"), "externalIds": trailer.get("externalIds")}
    trailers = route.get("trailers")
    if isinstance(trailers, list) and trailers:
        first = trailers[0]
        if isinstance(first, dict) and first.get("id"):
            return {"id": first.get("id"), "name": first.get("name"), "externalIds": first.get("externalIds")}
    return None


# ── Samsara API ──────────────────────────────────────────────────────────────

def fetch_routes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    _require("SAMSARA_TOKEN", cfg["token"])
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=ROUTES_LOOKBACK_MINUTES)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(minutes=ROUTES_LOOKAHEAD_MINUTES)).isoformat().replace("+00:00", "Z")
    url = f"{SAMSARA_BASE_URL}{SAMSARA_ROUTES_PATH}"

    all_routes: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    page = 0

    while True:
        page += 1
        if ROUTES_MAX_PAGES > 0 and page > ROUTES_MAX_PAGES:
            break
        params: dict[str, Any] = {"startTime": start, "endTime": end, "limit": ROUTES_PAGE_SIZE}
        if cursor:
            params["after"] = cursor

        r = requests.get(url, headers=cfg["headers"], params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        data = payload.get("data")
        if isinstance(data, list):
            all_routes.extend(x for x in data if isinstance(x, dict))
        elif isinstance(payload.get("routes"), list):
            all_routes.extend(x for x in payload["routes"] if isinstance(x, dict))

        pag = payload.get("pagination")
        if not isinstance(pag, dict):
            break
        cursor = pag.get("endCursor") if isinstance(pag.get("endCursor"), str) else None
        if not pag.get("hasNextPage") or not cursor:
            break

    return all_routes


# ── Webhook ──────────────────────────────────────────────────────────────────

def send_webhook(*, stop: dict[str, Any], eta_iso: str, route: dict[str, Any], cfg: dict[str, Any]) -> None:
    url = _require("WEBHOOK_URL", cfg["webhook_url"])
    tracking_url = _build_tracking_url(stop=stop, route=route)
    customer = _compute_customer_name(route)
    next_id, next_eta, next_mins = _route_next_upcoming_stop(route)

    try:
        mins_raw = minutes_until(eta_iso)
        mins = int(round(mins_raw))
    except Exception:
        mins = None

    notes = _stop_notes_text(stop)
    phone, phone_ext = _extract_phone(notes)

    payload = {
        "type": "eta_alert",
        "customerName": customer,
        "targetMinutes": TARGET_MINUTES,
        "windowMinutes": WINDOW_MINUTES,
        "minutesUntil": mins,
        "minutes": mins,
        "stop": {
            "id": stop.get("id"),
            "name": stop.get("name"),
            "externalIds": stop.get("externalIds"),
            "state": stop.get("state"),
            "phone": phone,
            "phoneExtension": phone_ext,
            "isNextStop": (str(stop.get("id")) == next_id) if stop.get("id") is not None else False,
        },
        "route": {
            "id": route.get("id"),
            "name": route.get("name"),
            "externalIds": route.get("externalIds"),
            "customerName": customer,
            "nextStopId": next_id,
            "nextStopEta": next_eta,
            "nextStopMinutesUntil": next_mins,
        },
        "vehicle": route.get("vehicle"),
        "driver": route.get("driver"),
        "trailer": _extract_trailer(route),
        "trackingUrl": tracking_url,
        "eta": eta_iso,
        "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    print(f"[webhook] sending stopId={stop.get('id')} minutesUntil={mins} customer={customer} phone={phone} eta={eta_iso}")
    r = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=20)
    r.raise_for_status()


# ── Main entry point ─────────────────────────────────────────────────────────

def main(event=None, context=None):
    _maybe_apply_context_secrets(context)
    cfg = _load_env()

    # Brief status line for hosted logs
    print(f"[secrets] ssm={os.environ.get('_ETA_ALERT_SECRETS_APPLIED') == '1'} "
          f"ctx={os.environ.get('_ETA_ALERT_CTX_SECRETS') == '1'} "
          f"token={bool(cfg['token'])} webhook={bool(cfg['webhook_url'])} "
          f"storage={os.environ.get('SamsaraFunctionStorageName', 'local')}")

    routes = fetch_routes(cfg)

    sent = 0
    skipped = {"already": 0, "no_eta": 0, "not_in_window": 0, "filtered": 0,
               "past": 0, "suppressed": 0, "not_next_stop": 0, "not_en_route": 0}

    tracked = []  # collect matching stops for summary log

    for route in routes:
        bypass = _route_force_include(route, patterns=cfg["force_include"])
        next_id, next_eta, next_mins = _route_next_upcoming_stop(route)
        route_name = route.get("name", "?")

        stops = route.get("stops")
        if not isinstance(stops, list):
            continue

        for stop in stops:
            if not isinstance(stop, dict):
                continue
            stop_id = stop.get("id")
            if not stop_id:
                continue

            if not _address_filter_passes(stop, allow=cfg["allow"], bypass=bypass):
                skipped["filtered"] += 1
                continue

            addr_name = _stop_address_name(stop)

            if _stop_is_completed(stop):
                skipped["past"] += 1
                continue

            eta_iso = _get_eta_iso(stop)
            if not eta_iso:
                skipped["no_eta"] += 1
                continue

            mins = minutes_until(eta_iso)
            if mins < 0:
                skipped["past"] += 1
                continue

            # Track this stop for the summary log
            ert = stop.get("enRouteTime")
            is_en_route = isinstance(ert, str) and bool(ert.strip())

            stop_key = str(stop_id)
            state_raw = get_item(stop_key)
            storage_failed = state_raw is STORAGE_ERROR
            state = {} if (state_raw is None or storage_failed) else state_raw

            if isinstance(state, dict) and state.get("notified") is True:
                skipped["already"] += 1
                tracked.append({"route": route_name, "addr": addr_name,
                                 "mins": round(mins, 1), "status": "already_sent",
                                 "en_route": is_en_route})
                continue

            if not _should_notify(stop_state=state, minutes_now=mins):
                skipped["not_in_window"] += 1
                set_item(stop_key, {
                    **(state if isinstance(state, dict) else {}),
                    "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                    "lastEta": eta_iso, "lastMinutes": mins, "notified": False,
                })
                tracked.append({"route": route_name, "addr": addr_name,
                                 "mins": round(mins, 1), "status": "approaching",
                                 "en_route": is_en_route})
                continue

            # En-route guard
            if REQUIRE_STOP_EN_ROUTE:
                ert = stop.get("enRouteTime")
                if not (isinstance(ert, str) and ert.strip()):
                    skipped["not_en_route"] += 1
                    set_item(stop_key, {
                        **(state if isinstance(state, dict) else {}),
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso, "lastMinutes": mins, "notified": False,
                    })
                    tracked.append({"route": route_name, "addr": addr_name,
                                     "mins": round(mins, 1), "status": "not_en_route",
                                     "en_route": False})
                    continue

            # Cold-start dedup: if /tmp was wiped (cold start) we have no stored
            # state, but a prior 5-min-ago invocation likely already sent this.
            # Guard: only alert if the stop became en-route recently (within
            # COLD_START_DEDUP_MINUTES).  If it's been en-route longer, skip
            # and persist the decision so persistent storage has the record.
            # IMPORTANT: Only apply if storage read succeeded (not on error).
            if not state and not storage_failed:
                er_mins = _en_route_minutes(stop)
                if er_mins is not None and er_mins > COLD_START_DEDUP_MINUTES:
                    skipped["already"] += 1
                    set_item(stop_key, {
                        "notified": True,
                        "coldStartDedup": True,
                        "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                        "lastEta": eta_iso, "lastMinutes": mins,
                    })
                    tracked.append({"route": route_name, "addr": addr_name,
                                     "mins": round(mins, 1), "status": "coldstart_dedup",
                                     "en_route": is_en_route})
                    continue

            # Next-stop guard
            if ALERT_ONLY_NEXT_STOP and next_id and str(stop_id) != str(next_id):
                skipped["not_next_stop"] += 1
                set_item(stop_key, {
                    **(state if isinstance(state, dict) else {}),
                    "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                    "lastEta": eta_iso, "lastMinutes": mins, "notified": False,
                })
                tracked.append({"route": route_name, "addr": addr_name,
                                 "mins": round(mins, 1), "status": "not_next_stop",
                                 "en_route": is_en_route})
                continue

            # Deny-list suppression
            if _address_is_suppressed(stop, deny=cfg["deny"]):
                skipped["suppressed"] += 1
                set_item(stop_key, {
                    **(state if isinstance(state, dict) else {}),
                    "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                    "lastEta": eta_iso, "lastMinutes": mins, "notified": False,
                })
                continue

            # Fire webhook
            try:
                send_webhook(stop=stop, eta_iso=eta_iso, route=route, cfg=cfg)
            except Exception as e:
                print(f"[webhook] FAILED stopId={stop_id}: {type(e).__name__}: {e}")
                # Don't update lastMinutes on failure — preserve crossing logic for retry
                set_item(stop_key, {
                    **(state if isinstance(state, dict) else {}),
                    "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                    "lastEta": eta_iso, "notified": False,
                    "webhookError": f"{type(e).__name__}: {e}",
                })
                tracked.append({"route": route_name, "addr": addr_name,
                                 "mins": round(mins, 1), "status": "webhook_failed",
                                 "en_route": is_en_route})
                continue

            set_item(stop_key, {
                **(state if isinstance(state, dict) else {}),
                "notified": True,
                "sentAt": datetime.now(timezone.utc).isoformat(),
                "eta": eta_iso,
                "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                "lastEta": eta_iso, "lastMinutes": mins,
            })
            sent += 1
            tracked.append({"route": route_name, "addr": addr_name,
                             "mins": round(mins, 1), "status": "SENT",
                             "en_route": is_en_route})

    # ── Sort tracked stops by ETA ──────────────────────────────────────
    tracked.sort(key=lambda t: t["mins"])

    # ── Cleanup stale entries ────────────────────────────────────────────
    cleaned = 0
    try:
        now_utc = datetime.now(timezone.utc)
        ttl_cutoff = now_utc - timedelta(hours=STORAGE_TTL_HOURS)
        for key in list_keys():
            try:
                entry = get_item(key)
                if not isinstance(entry, dict):
                    delete_item(key)
                    cleaned += 1
                    continue
                last_seen = entry.get("lastSeenAt") or entry.get("sentAt", "")
                if last_seen:
                    ts = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    if ts < ttl_cutoff:
                        delete_item(key)
                        cleaned += 1
            except Exception:
                pass
    except Exception as exc:
        print(f"[cleanup] error: {type(exc).__name__}: {exc}")
    if cleaned:
        print(f"[cleanup] purged {cleaned} stale entries (>{STORAGE_TTL_HOURS}h old)")

    result = {
        "status": "ok",
        "routes": len(routes),
        "sent": sent,
        "cleaned": cleaned,
        "tracked": tracked,
        "skipped": skipped,
        "config": {
            "target_minutes": TARGET_MINUTES,
            "window_minutes": WINDOW_MINUTES,
            "trigger_mode": TRIGGER_MODE,
            "require_en_route": REQUIRE_STOP_EN_ROUTE,
            "alert_only_next_stop": ALERT_ONLY_NEXT_STOP,
            "display_timezone": DISPLAY_TIMEZONE,
            "address_allow": cfg["ADDRESS_NAME_CONTAINS_ANY"],
            "address_deny": cfg["ADDRESS_NAME_EXCLUDES_ANY"],
            "route_force_include": cfg["ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY"],
        },
    }
    return result


# ── CLI runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import time
    import traceback

    parser = argparse.ArgumentParser(description="ETA Alert")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between runs")
    args = parser.parse_args()

    if not args.loop:
        print(main())
    else:
        while True:
            print(f"--- {datetime.now(timezone.utc).isoformat()}Z ---")
            try:
                print(main())
            except Exception:
                traceback.print_exc()
            time.sleep(max(5, args.interval))
