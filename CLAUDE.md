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
  templates/          — Jinja2 templates (base, login, verify, send, sent, privacy, terms)
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
export FLYCTL_INSTALL="/home/user/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"

# Authenticate — either interactively or with a token:
fly auth login
# or: export FLY_API_TOKEN="FlyV1 ..."
```

## Google Spreadsheet

The spreadsheet serves as the admin interface — non-technical clerks manage contacts by editing it directly.

- **Service account key:** `woodlawn-sms-d74fa6940b5b.json` (in project root, git-ignored)
- **Spreadsheet ID:** Set as `SPREADSHEET_ID` Fly secret (also in `setup_sheet.py` locally)

### Tab layout

Three tabs, each with the same structure:
- **Row 1:** Tab name + explanation text
- **Row 2:** Headers (`Name` | `Phone`)
- **Row 3+:** Data

| Tab | Purpose |
|-----|---------|
| **Recipients** | Everyone who receives real broadcast messages |
| **Test** | Numbers that receive test-mode messages |
| **Admins** | Phone numbers authorized to sign in and send |

The app reads from row 3 onward (skips the explanation and header rows). Phone numbers are in column B and are normalized to E.164 format (`+1XXXXXXXXXX`).

## Key design decisions

- **Single file app.** Everything is in `app.py` — this is intentionally simple, not a candidate for splitting into modules.
- **OTP via plain SMS**, not Twilio Verify (cheaper, no extra service).
- **In-memory OTP storage** — codes expire after 5 minutes. Fine because the OTP flow is fast and the app is single-instance.
- **Message length cap: 133 chars** — leaves room for the `\nReply STOP to unsubscribe` suffix within a single 160-char SMS segment.
- **JS confirm dialog** for sending to real recipients (not a separate confirmation page).
- **30-day sessions** via Flask signed cookies.
- **Toll-free number** — avoids the complexity of A2P 10DLC registration for local numbers.

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
