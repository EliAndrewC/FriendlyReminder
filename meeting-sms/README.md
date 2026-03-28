# Meeting SMS

A simple SMS broadcast tool for a Quaker Meeting (or any small community). Clerks
sign in with their phone number, compose a short message, and send it to the
community via Twilio. The contact list lives in a Google Spreadsheet.

## Google Spreadsheet setup

Create a Google Spreadsheet with three sheets (tabs):

**Recipients** — people who receive messages:
| Name | Phone |
|------|-------|
| Jane Doe | (555) 123-4567 |

**Test** — numbers for testing (probably just your own):
| Name | Phone |
|------|-------|
| You | (555) 987-6543 |

**Admins** — phone numbers allowed to sign in and send:
| Name | Phone |
|------|-------|
| You | (555) 987-6543 |

Each sheet needs a header row. Phone numbers go in column B.

## Google Cloud service account

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a
   project (or use an existing one).
2. Enable the **Google Sheets API**.
3. Go to **IAM & Admin > Service Accounts** and create a service account.
4. Create a JSON key for the service account and download it.
5. Share your spreadsheet with the service account's email address
   (something like `name@project.iam.gserviceaccount.com`) as a **Viewer**.

## Twilio setup

1. Create a [Twilio](https://www.twilio.com/) account.
2. Get a phone number. A standard US local number works.
3. Note your **Account SID**, **Auth Token**, and the Twilio phone number.

**Important:** US carriers now require A2P 10DLC registration for business
messaging on local numbers. In the Twilio console you'll need to register your
brand and campaign. For a small non-profit sending occasional alerts, this is
straightforward and typically approved quickly. Alternatively, a toll-free number
has a simpler verification process.

## Environment variables

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | A random string for Flask session signing. Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `TWILIO_ACCOUNT_SID` | From Twilio console |
| `TWILIO_AUTH_TOKEN` | From Twilio console |
| `TWILIO_FROM_NUMBER` | Your Twilio phone number in E.164 format, e.g. `+15551234567` |
| `SPREADSHEET_ID` | The ID from your spreadsheet URL: `docs.google.com/spreadsheets/d/{THIS_PART}/edit` |
| `GOOGLE_CREDENTIALS` | The full JSON content of your service account key file |

## Deploy to Fly.io

```bash
# Install flyctl if needed: https://fly.io/docs/hands-on/install-flyctl/

cd meeting-sms
fly launch          # follow the prompts, say no to databases

# Set secrets (Fly encrypts these)
fly secrets set SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fly secrets set TWILIO_ACCOUNT_SID="AC..."
fly secrets set TWILIO_AUTH_TOKEN="..."
fly secrets set TWILIO_FROM_NUMBER="+15551234567"
fly secrets set SPREADSHEET_ID="1abc..."
fly secrets set GOOGLE_CREDENTIALS="$(cat service-account-key.json)"

fly deploy
```

The app scales to zero when idle (`min_machines_running = 0`), so you'll only
pay for the few seconds it's active when someone sends a message — essentially
free on Fly.io's free tier.

## Run locally

```bash
pip install -r requirements.txt

export SECRET_KEY="dev"
export TWILIO_ACCOUNT_SID="AC..."
export TWILIO_AUTH_TOKEN="..."
export TWILIO_FROM_NUMBER="+15551234567"
export SPREADSHEET_ID="1abc..."
export GOOGLE_CREDENTIALS="$(cat service-account-key.json)"

flask --app app run --port 8080
```

## Notes

- **STOP handling:** Twilio automatically honors STOP/START replies on US
  numbers. If someone texts STOP, Twilio won't deliver future messages to them.
  They can text START to re-opt-in. The app appends "Reply STOP to unsubscribe"
  to each message.
- **Message length:** Messages are capped at 133 characters so the full SMS
  (including the STOP suffix) fits in a single 160-character SMS segment.
- **Session duration:** Sign-in sessions last 30 days.
- **OTP storage:** Verification codes are stored in memory and expire after
  5 minutes. Since the app scales to zero on Fly.io, codes won't survive a
  machine stop — but the OTP flow is fast enough that this isn't a problem in
  practice.
