# ETA Alert (Samsara Function)

Sends a **webhook** when a truck is ~60 minutes from arriving at a stop.

## How it works

1. Polls Samsara `/fleet/routes` on a schedule (e.g. every 5 minutes)
2. For each stop whose ETA crosses the 60-minute threshold, fires a POST to your webhook URL
3. Uses JSON-backed storage (keyed by `stopId`) to ensure each stop only triggers once
4. Only alerts for stops that are **en-route** and are the **next upcoming stop** on the route

## Files

| File | Purpose |
|------|---------|
| `eta-alert/main.py` | Core logic — polling, filtering, webhook delivery |
| `eta-alert/storage.py` | Simple JSON KV store (uses `/tmp` in serverless) |
| `eta-alert/requirements.txt` | Python dependencies |
| `eta-alert/.env` | Local dev secrets (never deploy this) |
| `main.py` (root) | Handler shim so Samsara Functions can use `main.main` |
| `function.py` (root) | Handler shim for `function.main` |

## Secrets / Environment Variables

Only **5 variables** are needed — set them as Samsara Function Secrets:

| Variable | Required | Description |
|----------|----------|-------------|
| `SAMSARA_TOKEN` | **Yes** | Samsara API bearer token |
| `WEBHOOK_URL` | **Yes** | Destination URL for POST payloads |
| `ADDRESS_NAME_CONTAINS_ANY` | No | Comma-separated allowlist substrings (case-insensitive) |
| `ADDRESS_NAME_EXCLUDES_ANY` | No | Comma-separated denylist substrings |
| `ROUTE_FORCE_INCLUDE_ON_STOP_ADDRESS_CONTAINS_ANY` | No | If any stop on a route matches, bypass the allowlist for the whole route |

Everything else (target minutes, timezone, trigger mode, guards, etc.) is **hardcoded** in `main.py`.

## Hardcoded Defaults

| Setting | Value |
|---------|-------|
| Target ETA | 60 minutes |
| Window | ±5 minutes |
| Timezone (display) | America/Chicago |
| Trigger mode | crossing (alerts once as ETA drops below target) |
| En-route guard | ON (only alerts if stop has `enRouteTime`) |
| Next-stop guard | ON (only alerts for the next upcoming stop) |
| Routes lookback | 7 days |
| Routes lookahead | 7 days |

## Deploying to Samsara Functions

1. Set **Handler** to `main.main`
2. Add the 5 secrets above in the Functions dashboard
3. Configure a schedule (e.g. every 5 minutes)
4. Do **not** bundle `eta-alert/.env` or `.function_storage.json`

## Local Development

```bash
cd eta-alert
pip install -r requirements.txt
# Edit .env with your real SAMSARA_TOKEN and WEBHOOK_URL
python main.py
```

## Webhook Payload

```json
{
  "type": "eta_alert",
  "customerName": "Bomgaars",
  "targetMinutes": 60,
  "windowMinutes": 5,
  "minutesUntil": 58,
  "minutes": 58,
  "stop": { "id": "...", "name": "...", "phone": "+15551234567", "isNextStop": true },
  "route": { "id": "...", "name": "...", "customerName": "Bomgaars", "nextStopId": "..." },
  "vehicle": { "id": "...", "name": "..." },
  "driver": { "id": "...", "name": "..." },
  "trailer": { "id": "...", "name": "..." },
  "trackingUrl": "https://...",
  "eta": "2025-01-15T14:30:00Z",
  "sentAt": "2025-01-15T13:32:00Z"
}
```
