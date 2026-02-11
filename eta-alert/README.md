# ETA Alert (Samsara Function)

Send email alerts to customers when a truck is ~60 minutes from arriving.

## What it does

- Polls Samsara routes/stops on a schedule (recommended: every 5 minutes)
- For each stop with an ETA inside a configurable window around 60 minutes, sends an email
- Uses persistent storage (keyed by `stopId`) to ensure each stop only triggers once
- Optionally calls your internal endpoint to look up customer email when it is not present on the stop

You can also switch to an *incremental* mode using `/fleet/routes/audit-logs/feed` (see **DATA_SOURCE** below).

## Files

- `main.py` — function entrypoint: `main.main`
- `storage.py` — simple JSON-backed KV store (works locally; can be pointed at a durable path)
- `requirements.txt` — Python deps
- `.env.example` — env/secret names

## Deploying to Samsara Functions (recommended)

This project is designed to run as a **scheduled** Samsara Function (not as a long-running process).

- In Samsara Functions, configure a schedule (e.g. every 5 minutes).
- Set the **Handler** to `main.main`.
- Do **not** run with `--loop` inside Samsara Functions; the platform scheduler triggers each run.

Important: this repo includes a small root-level [main.py](../main.py) shim so hosted runtimes can import `main.main`. The application code lives under `eta-alert/`.

### Bundling safely (avoid leaking secrets)

If you use the `samsara-fn` CLI to bundle a zip, make sure you **do not include** local secret files:

- Do not bundle `eta-alert/.env` (local secrets).
- Do not bundle `.function_storage*.json` (local state) unless you intentionally want to seed state.

Tip: if the bundler warns about potentially sensitive files, remove them from the folder before bundling and rely on **Samsara Secrets** in the dashboard.

## Secrets / env vars

Minimum:

- `SAMSARA_TOKEN` — Samsara API token
- Email (pick one) if you use `NOTIFY_MODE=email` or `both`:
  - Postmark: `EMAIL_API_KEY` (Postmark Server Token)
  - SendGrid: `SENDGRID_API_KEY`
  - Outlook (Microsoft 365 via Microsoft Graph): see **Outlook / Microsoft Graph** below

Webhook if you use `NOTIFY_MODE=webhook` or `both`:

- `WEBHOOK_URL`
- Optional: `WEBHOOK_HEADERS_JSON` (JSON object of extra headers)

Recommended:

- `EMAIL_FROM` — sender address
- `EMAIL_PROVIDER` — `postmark` (default), `sendgrid`, or `outlook`
- `NOTIFY_MODE` — `email` (default), `webhook`, or `both`

Optional:

- `CUSTOMER_LOOKUP_URL_TEMPLATE` — e.g. `https://internal-api/stops/{stop_id}` returning JSON like `{ "email": "a@b.com" }`
- `EMAIL_REPLY_TO` — optional Reply-To address
- `EMAIL_SUBJECT` — optional subject override; supports `{stop}`, `{route}`, `{minutes}`, `{eta}`

  It also supports `{customer}` (e.g. `Gerkin`) and `{customer_tag}` (either `" (Gerkin)"` or empty).
- `EMAIL_TO_OVERRIDE` — optional override recipient; if set, **all** emails go to this address (useful for testing)
- `TARGET_MINUTES` — default `60`
- `WINDOW_MINUTES` — default `5` (triggers in `[TARGET - WINDOW, TARGET + WINDOW)` minutes)

### Webhook-only (recommended)

If you only want webhooks (no email), set these secrets:

- `SAMSARA_TOKEN`
- `NOTIFY_MODE=webhook`
- `WEBHOOK_URL`

And copy any filtering/tuning vars you use locally (e.g. `ADDRESS_NAME_CONTAINS_ANY`, `ADDRESS_NAME_EXCLUDES_ANY`, `ROUTES_LOOKBACK_MINUTES`, etc.).

Do not set email secrets (`OUTLOOK_*`, `SENDGRID_API_KEY`, `EMAIL_API_KEY`, etc.) if you don't want email.

## Outlook / Microsoft Graph

This repo supports sending the alert email via Microsoft 365 (Outlook) using Microsoft Graph (app-only / client-credentials flow).

1) Create an Azure AD app registration

- Azure Portal → Microsoft Entra ID → App registrations → New registration

2) Create a client secret

- Certificates & secrets → New client secret

3) Grant Graph permissions

- API permissions → Add a permission → Microsoft Graph → **Application permissions**
- Add: `Mail.Send`
- Click **Grant admin consent**

4) Set env vars

- `EMAIL_PROVIDER=outlook`
- `OUTLOOK_TENANT_ID` — your tenant ID (GUID)
- `OUTLOOK_CLIENT_ID` — app (client) ID
- `OUTLOOK_CLIENT_SECRET` — the secret value
- `OUTLOOK_SENDER` — mailbox to send *from* (email address or user id)

Notes:

- The implementation uses Graph `POST /v1.0/users/{OUTLOOK_SENDER}/sendMail`.
- If your org restricts which mailboxes an app can send as, you may need an Exchange Online Application Access Policy.
- Emails are sent as HTML (with plain-text fallback for providers that support it).

Trigger behavior:

- `TRIGGER_MODE`
  - `crossing` (default): sends **once per stop** when ETA crosses from `> TARGET_MINUTES` to `<= TARGET_MINUTES`
  - `window`: sends **once per stop** when ETA is inside `[TARGET - WINDOW, TARGET + WINDOW)`
- `TRIGGER_REQUIRE_CROSSING`
  - `0` (default): if the first time we ever see a stop it’s already within the window, we’ll still alert
  - `1`: only alert if we previously saw the stop with ETA `> TARGET_MINUTES` (strict “pass the mark”)

- `TRIGGER_NO_HISTORY_MODE` (only applies when `TRIGGER_MODE=crossing` and there is no prior history for the stop)
  - `window` (default): alert only if the first-seen ETA is inside `[TARGET - WINDOW, TARGET + WINDOW)`
  - `below_target`: alert if the first-seen ETA is `<= TARGET`
  - `none`: never alert without history

Filtering (address/location name):

- `ADDRESS_NAME_CONTAINS_ANY` — comma-separated substrings; if set, only matching stops are considered
- `ADDRESS_NAME_EXCLUDES_ANY` — comma-separated substrings; if set, matching stops are skipped

Route-level inclusion (include the whole route if any stop matches):

- `ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY` — comma-separated substrings; if any stop on a route matches, the app will bypass the stop allowlist for the rest of that route’s stops (denylist still applies)

Data source (choose one):

- `DATA_SOURCE` — `routes` (default) or `audit_logs`
  - `routes`: simplest + most reliable (checks all stops every run) — recommended
  - `audit_logs`: more efficient (consumes incremental route changes via audit-log feed)

Note: in the Samsara audit-log feed we tested, entries did not include predicted stop ETAs, so `DATA_SOURCE=audit_logs` is best treated as an optional mode for route state-change processing rather than the primary way to compute “minutes until arrival”.

If `DATA_SOURCE=audit_logs`, these env vars apply:

- `SAMSARA_AUDIT_LOGS_PATH` — default `/fleet/routes/audit-logs/feed`
- `AUDIT_LOGS_CURSOR_PARAM` — default `after` (cursor param name)
- `AUDIT_LOGS_PAGE_SIZE_PARAM` — default `limit`
- `AUDIT_LOGS_PAGE_SIZE` — default `200`
- `AUDIT_LOGS_MAX_PAGES_PER_RUN` — default `10`

`/fleet/routes` time window (required by Samsara):

- `ROUTES_START_TIME` / `ROUTES_END_TIME` — optional explicit RFC3339 timestamps
- Or let the function compute them:
  - `ROUTES_LOOKBACK_MINUTES` — default `10080` (7 days; helps include multi-day routes)
  - `ROUTES_LOOKAHEAD_MINUTES` — default `10080` (7 days)
- Pagination safety knobs:
  - `ROUTES_PAGE_SIZE` — default `512` (max)
  - `ROUTES_MAX_PAGES_PER_RUN` — default `5`
  - `ROUTES_INCLUDE` — optional (comma-separated) include values supported by Samsara

## Storage behavior

Deduping is based on `stopId`:

- If a stop has already sent an alert, it is skipped forever
- Local runs store state in `eta-alert/.function_storage.json`
- In a hosted environment, set `FUNCTION_STORAGE_PATH` to a durable path if your runtime requires it

When `DATA_SOURCE=audit_logs`, the function also stores the last processed feed cursor under a special key so it can resume where it left off on the next run.

## Run locally (sanity check)

From the workspace root:

1) Create a virtualenv and install deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r .\eta-alert\requirements.txt
```

2) Set environment variables (or copy `.env.example` into your preferred env loader)

```powershell
$env:SAMSARA_TOKEN = "..."
$env:EMAIL_API_KEY = "..."   # Postmark
```

Note: `eta-alert/main.py` will load `eta-alert/.env` if present, but by default it will **not override** environment variables you've already set (set `DOTENV_OVERRIDE=1` to force `.env` to win).

3) Run the handler

```powershell
C:/Users/nnrub/.vscode/ETA_ALERT/.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, 'eta-alert'); import main; print(main.main())"
```

### Zero-credential quick test (recommended first)

This tests the 60-minute window + dedupe logic without calling Samsara and without sending email:

```powershell
$env:USE_SAMPLE_DATA = "1"
$env:DRY_RUN = "1"
C:/Users/nnrub/.vscode/ETA_ALERT/.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, 'eta-alert'); import main; print(main.main())"
```

### Webhook sample (end-to-end)

1) Create a temporary webhook URL (for testing)

- https://webhook.site (recommended)

2) Run the function in webhook mode with sample data

```powershell
$env:USE_SAMPLE_DATA = "1"        # no Samsara API calls
$env:DRY_RUN = "0"                # actually sends the webhook
$env:NOTIFY_MODE = "webhook"
$env:WEBHOOK_URL = "https://webhook.site/<your-id>"

C:/Users/nnrub/.vscode/ETA_ALERT/.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, 'eta-alert'); import main; print(main.main())"
```

3) What you’ll receive

Your endpoint receives JSON like:

```json
{
  "type": "eta_alert",
  "targetMinutes": 60,
  "windowMinutes": 5,
  "stop": {
    "id": "141414",
    "name": "Stop #1",
    "externalIds": {},
    "state": "scheduled"
  },
  "route": {
    "id": "342341",
    "name": "Bid 123",
    "externalIds": {}
  },
  "vehicle": {"id": "494123", "name": "Fleet Truck #1"},
  "driver": {"id": "45646", "name": "Driver Bob"},
  "eta": "2026-01-14T21:15:00Z",
  "sentAt": "2026-01-14T20:15:00Z"
}
```

(If you use the `samsara-fn` CLI, you can invoke the handler directly through it; see your internal standard workflow.)

## Deploy (Samsara Functions)

- Bundle/upload the folder `eta-alert/`
- Set handler to `main.main`
- Add secrets listed above
- Schedule every 5 minutes

## Notes

- If your tenant uses a different routes endpoint or response shape, set `SAMSARA_ROUTES_PATH` and/or adjust parsing in `fetch_routes()`.
- If you use `audit_logs`, you may not see an entry for a stop unless Samsara emits an update (e.g., ETA changes). If you need “never miss the 60-minute window” behavior, keep using `routes` polling.
- For higher reliability, you may want to widen `WINDOW_MINUTES` slightly if ETAs shift quickly or runs are delayed.
