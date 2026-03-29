# Meeting SMS

SMS broadcast tool for Alexandria Friends Meeting. Clerks sign in via phone number OTP, compose a short message, and send it to the community via Twilio.

## Stack

- Python / Flask (single file: `app.py`)
- Google Sheets API via `gspread` for contact management
- Twilio for SMS (both OTP auth and broadcast)
- Deployed on Fly.io (scales to zero)
- Gunicorn as WSGI server
- Docker (Python 3.12-slim)

## Project structure

```
meeting-sms/
  app.py              — All application logic (routes, auth, SMS sending)
  test_app.py         — pytest test suite (mocks Twilio and Google Sheets)
  templates/          — Jinja2 templates (base, login, verify, send, sent, voice, voice_sent, privacy, terms)
  static/style.css    — Minimal CSS
  static/guestbook.pdf — Served at /guestbook.pdf
  Dockerfile          — Python 3.12 slim image, gunicorn on port 8080
  fly.toml            — Fly.io config (app: ammsms, region: iad, scales to zero)
  requirements.txt    — Pinned to major versions (flask 3.1, twilio 9, etc.)

setup_sheet.py      — One-time script that formatted the Google Spreadsheet tabs (already run, not part of the deployed app)
woodlawn-sms-d74fa6940b5b.json — Google service account key file (do NOT commit or deploy; contents are set as a Fly secret)
```

## Fly.io deployment

- **App name:** `ammsms`
- **URL:** https://ammsms.fly.dev
- **Region:** iad (US East)
- **Scaling:** scales to zero (`min_machines_running = 0`), auto-starts on request
- **VM:** shared CPU, 1 GB RAM

### Deploying

```bash
cd /workspace/meeting-sms
fly deploy
```

### Secrets

All config is stored as Fly secrets (not in files). To update:

```bash
fly secrets set KEY="value"
```

Current secrets:
- `SECRET_KEY` — Flask session signing key
- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `TWILIO_FROM_NUMBER` — Twilio sending number (currently `+18888144284`, a toll-free number)
- `SPREADSHEET_ID` — Google Spreadsheet ID
- `GOOGLE_CREDENTIALS` — Full JSON of the Google service account key

### Container bootstrap

If developing in a fresh container (e.g. new Claude Code session):

```bash
# Install flyctl (may already be installed)
curl -L https://fly.io/install.sh | sh
export FLYCTL_INSTALL="/home/agent/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"
```

The Fly API token and other secrets are stored in `/workspace/.env`. **Do not `source` this file** — it contains unquoted JSON and base64 values that break shell parsing. Instead, extract `FLY_API_TOKEN` directly:

```bash
export FLY_API_TOKEN=$(grep '^FLY_API_TOKEN=' /workspace/.env | cut -d= -f2-)
```

Verify with `fly status` from the `meeting-sms/` directory.

## Google Spreadsheet

The spreadsheet serves as the admin interface — non-technical clerks manage contacts by editing it directly.

- **Service account key:** The JSON key is stored as the `GOOGLE_CREDENTIALS` Fly secret (and in `/workspace/.env`). The original file `woodlawn-sms-d74fa6940b5b.json` is git-ignored and may not be present in fresh containers.
- **Spreadsheet ID:** Set as `SPREADSHEET_ID` Fly secret (and in `/workspace/.env`).

### Accessing the spreadsheet programmatically

Both `GOOGLE_CREDENTIALS` and `SPREADSHEET_ID` are available in `/workspace/.env`. Extract them with `cut` (do not `source` the file — see Container bootstrap above):

```bash
export SPREADSHEET_ID=$(grep '^SPREADSHEET_ID=' /workspace/.env | cut -d= -f2-)
export GOOGLE_CREDENTIALS=$(grep '^GOOGLE_CREDENTIALS=' /workspace/.env | cut -d= -f2-)
```

Then use `gspread` in Python:

```python
import json, os, gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_CREDENTIALS"]),
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
ws = sheet.worksheet("Recipients")  # or "Test", "Admins"
```

Use the read-write scope (`spreadsheets` not `spreadsheets.readonly`) when you need to update cells. The `gspread` API provides `ws.get_all_values()`, `ws.update_cell(row, col, value)`, `ws.append_row([...])`, etc.

### Tab layout

Three tabs, each with the same structure:
- **Row 1:** Tab name + explanation text
- **Row 2:** Headers (`Name` | `Phone` | `Voice` | `Opted Out` | `Opt-Out Date`)
- **Row 3+:** Data

| Tab | Purpose |
|-----|---------|
| **Recipients** | Everyone who receives real broadcast messages |
| **Test** | Numbers that receive test-mode messages |
| **Admins** | Phone numbers authorized to sign in and send |

| Column | Description |
|--------|-------------|
| **A: Name** | Contact name |
| **B: Phone** | Phone number, normalized to E.164 (`+1XXXXXXXXXX`) |
| **C: Voice** | Checkbox — if checked, contact receives voice calls instead of SMS |
| **D: Opted Out** | Checkbox — checked when someone presses 9 to unsubscribe from voice calls (or manually by a clerk) |
| **E: Opt-Out Date** | Date auto-filled by the app when someone opts out via keypad; empty if manually opted out |

The app reads from row 3 onward (skips the explanation and header rows). The Admins tab only uses columns A and B.

## Key design decisions

- **Single file app.** Everything is in `app.py` — this is intentionally simple, not a candidate for splitting into modules.
- **OTP via plain SMS**, not Twilio Verify (cheaper, no extra service).
- **In-memory OTP storage** — codes expire after 5 minutes. Fine because the OTP flow is fast and the app is single-instance.
- **Message length cap: 133 chars** — leaves room for the `\nReply STOP to unsubscribe` suffix within a single 160-char SMS segment.
- **Voice calls for landlines** — contacts with the Voice checkbox get TTS calls instead of SMS. Separate form with no character limit. Calls include "press 9 to unsubscribe" which writes the opt-out back to the spreadsheet.
- **Voice opt-out webhook** (`/voice-optout`) — Twilio POSTs here when a call recipient presses a key. Requires the `APP_URL` env var (defaults to `https://ammsms.fly.dev`). Google Sheets scope is read-write to support this.
- **JS confirm dialog** for sending to real recipients (not a separate confirmation page).
- **30-day sessions** via Flask signed cookies.
- **Toll-free number** — avoids the complexity of A2P 10DLC registration for local numbers.

## Testing and linting

Use **test-driven development** for bugfixes and new features: write failing tests first, then implement until they pass.

### Running tests

```bash
cd /workspace/meeting-sms
python3 -m pytest test_app.py -v
```

With coverage report:

```bash
python3 -m pytest test_app.py --cov=app --cov-report=term-missing
```

External services (Twilio, Google Sheets) are mocked in tests — no credentials needed to run the suite.

### Formatting

Code is formatted with **black**. Run it before committing:

```bash
python3 -m black app.py test_app.py
```

## Running locally

```bash
cd /workspace/meeting-sms
pip install -r requirements.txt

export SECRET_KEY="dev"
export TWILIO_ACCOUNT_SID="..."
export TWILIO_AUTH_TOKEN="..."
export TWILIO_FROM_NUMBER="+18888144284"
export SPREADSHEET_ID="..."
export GOOGLE_CREDENTIALS="$(cat woodlawn-sms-d74fa6940b5b.json)"

flask --app app run --port 8080
```
