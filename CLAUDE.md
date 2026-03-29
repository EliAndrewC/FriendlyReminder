# Meeting SMS

SMS and voice broadcast tool for Alexandria Friends Meeting. Clerks sign in via phone number OTP, compose a message, and send it to the community via Twilio (SMS to cell phones, voice calls to landlines). Also handles incoming SMS replies and voicemails with transcription.

## Stack

- Python / Flask (single file: `app.py`)
- Google Sheets API via `gspread` for contact management and message logging
- Twilio for SMS (OTP auth, broadcast, incoming replies) and voice (outbound calls, voicemail, transcription)
- Deployed on Fly.io (scales to zero)
- Gunicorn as WSGI server
- Docker (Python 3.12-slim)

## Project structure

```
meeting-sms/
  app.py              — All application logic (routes, auth, SMS/voice sending, webhooks)
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

Optional env var (has a default):
- `APP_URL` — Base URL for webhook callbacks in TwiML (defaults to `https://ammsms.fly.dev`). Used by voice opt-out `<Gather>` action, recording-complete action, and transcription callback.

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

Six tabs. All tabs have row 1 as an explanation and row 2 as headers. Data starts at row 3.

### Recipients / Test tabs

| Column | Description |
|--------|-------------|
| **A: Name** | Contact name |
| **B: Phone** | Phone number, normalized to E.164 (`+1XXXXXXXXXX`) |
| **C: Voice call instead of SMS** | Checkbox — if checked, contact receives voice calls instead of SMS |
| **D: Opted Out** | Checkbox — checked when someone presses 9 to unsubscribe from voice calls (or manually by a clerk) |
| **E: Opt-Out Date** | Date auto-filled by the app when someone opts out via keypad; empty if manually opted out |

**Recipients** are used for real broadcasts. **Test** numbers receive test-mode messages only.

### Admins tab

| Column | Description |
|--------|-------------|
| **A: Name** | Admin name |
| **B: Phone** | Phone number authorized to sign in and send |
| **C: Notify about replies** | Checkbox — if checked, admin receives an SMS notification when an incoming SMS reply or voicemail is received (rate-limited to one notification per hour) |

### SMS Replies tab (auto-populated)

| Column | Description |
|--------|-------------|
| **A: Phone** | Sender's phone number |
| **B: Name** | Sender's name (looked up from Recipients/Test/Admins; empty if unknown) |
| **C: Date/Time** | Timestamp when the reply was received |
| **D: Message** | The text message body |

### Voicemails tab (auto-populated)

| Column | Description |
|--------|-------------|
| **A: Phone** | Caller's phone number |
| **B: Name** | Caller's name (looked up from Recipients/Test/Admins; empty if unknown) |
| **C: Date/Time** | Timestamp when the voicemail was left |
| **D: Duration** | Recording length in seconds |
| **E: Recording** | Link to play the audio (proxied through the app at `/recording/<SID>`; requires login) |
| **F: Transcription** | Auto-transcribed text from Twilio (async, may take a minute or two after the call) |

### Message Log tab (auto-populated)

| Column | Description |
|--------|-------------|
| **A: Date/Time** | Timestamp when the broadcast was sent |
| **B: Mode** | `test` or `real` |
| **C: Type** | `SMS` or `Voice` |
| **D: Recipients** | Number of messages/calls successfully sent |
| **E: Message** | The message text |

The app reads from row 3 onward (skips the explanation and header rows).

## Key design decisions

- **Single file app.** Everything is in `app.py` — this is intentionally simple, not a candidate for splitting into modules.
- **OTP via plain SMS**, not Twilio Verify (cheaper, no extra service).
- **In-memory OTP storage** — codes expire after 5 minutes. Fine because the OTP flow is fast and the app is single-instance.
- **Message length cap: 133 chars** — leaves room for the `\nReply STOP to unsubscribe` suffix within a single 160-char SMS segment.
- **`$NAME` substitution** — messages can include `$NAME` which is replaced per-contact with their name from column A. Works in both SMS and voice messages.
- **Voice calls for landlines** — contacts with the Voice checkbox get TTS calls instead of SMS. Separate form with no character limit. Calls include "press 9 to unsubscribe" which writes the opt-out back to the spreadsheet.
- **Voice opt-out webhook** (`/voice-optout`) — Twilio POSTs here when a call recipient presses a key. Requires the `APP_URL` env var (defaults to `https://ammsms.fly.dev`). Google Sheets scope is read-write to support this.
- **Incoming SMS replies** — Twilio forwards incoming texts to `/sms-reply`, which logs them to the SMS Replies tab. Twilio webhook configured via API on the toll-free number.
- **Voicemail with transcription** — Incoming calls to the toll-free number are handled by `/incoming-call`, which plays a greeting and records a voicemail. `/recording-complete` logs it to the Voicemails tab. `/transcription` receives the async transcription from Twilio and updates the row.
- **Recording proxy** — Voicemail audio is stored on Twilio's servers (protected by HTTP Basic Auth). The app proxies playback through `/recording/<SID>` so clerks can listen from the spreadsheet without needing Twilio credentials. Requires login.
- **Admin reply notifications** — When an incoming SMS or voicemail arrives, admins with "Notify about replies" checked receive an SMS notification. Rate-limited to one notification per hour (checks timestamps of the most recent entries in SMS Replies and Voicemails tabs before the new row is written).
- **Outgoing message log** — Every SMS and voice broadcast is logged to the Message Log tab with timestamp, mode, type, recipient count, and message text.
- **Twilio webhook signature validation** — All Twilio webhook routes (`/sms-reply`, `/voice-optout`, `/incoming-call`, `/recording-complete`, `/transcription`) validate the `X-Twilio-Signature` header using `RequestValidator`. Skipped when `app.config["TESTING"]` is True.
- **JS confirm dialog** for sending to real recipients (not a separate confirmation page).
- **30-day sessions** via Flask signed cookies.
- **Toll-free number** (`+18888144284`) — avoids the complexity of A2P 10DLC registration for local numbers. A local 571/703 number is being registered separately for better voice call deliverability (toll-free numbers are often spam-filtered by carriers for outbound voice).

## Twilio webhook configuration

Both webhooks are configured via the Twilio API on the toll-free number (`+18888144284`):

- **SMS webhook:** `https://ammsms.fly.dev/sms-reply` (POST) — receives incoming text replies
- **Voice webhook:** `https://ammsms.fly.dev/incoming-call` (POST) — handles incoming calls with voicemail

These can be viewed/updated via the Twilio Console under Phone Numbers > Active Numbers, or via the API.

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
export APP_URL="https://your-ngrok-url.ngrok.io"  # needed for Twilio webhook callbacks when testing locally

flask --app app run --port 8080
```

Note: Twilio webhooks (`/sms-reply`, `/incoming-call`, etc.) require a publicly accessible URL. For local development, use a tool like `ngrok` to tunnel, and set `APP_URL` to the ngrok URL.
