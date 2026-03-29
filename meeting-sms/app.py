import json
import os
import random
import time
from datetime import date, timedelta
from functools import wraps

import re

import gspread
import requests as http_requests
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from google.oauth2.service_account import Credentials
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)

twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
)
TWILIO_FROM = os.environ["TWILIO_FROM_NUMBER"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

STOP_SUFFIX = "\nReply STOP to unsubscribe"
MAX_MESSAGE_LENGTH = 160 - len(STOP_SUFFIX)

# In-memory OTP storage: {phone: {"code": str, "expires": float}}
pending_otps = {}


def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def _is_true(val):
    return val.strip().upper() == "TRUE" if val else False


def get_contacts(sheet_name):
    """Get contacts from a named sheet with voice and opt-out status.

    Returns list of dicts: {name, phone, voice, opted_out}.
    Columns: A=Name, B=Phone, C=Voice, D=Opted Out, E=Opt-Out Date.
    Rows with missing C/D columns default to SMS (voice=False) and not opted out.
    """
    worksheet = get_sheet().worksheet(sheet_name)
    rows = worksheet.get_all_values()
    contacts = []
    for row in rows[2:]:  # Skip explanation row and header row
        if len(row) < 2 or not row[1].strip():
            continue
        contacts.append(
            {
                "name": row[0].strip() if len(row) > 0 else "",
                "phone": normalize_phone(row[1].strip()),
                "voice": _is_true(row[2]) if len(row) > 2 else False,
                "opted_out": _is_true(row[3]) if len(row) > 3 else False,
            }
        )
    return contacts


def get_phone_numbers(sheet_name):
    """Get phone numbers from a named sheet (legacy helper for Admins tab)."""
    worksheet = get_sheet().worksheet(sheet_name)
    rows = worksheet.get_all_values()
    return [row[1].strip() for row in rows[2:] if len(row) > 1 and row[1].strip()]


def normalize_phone(phone):
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "phone" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def validate_twilio_request(f):
    """Verify that incoming webhook requests are actually from Twilio."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if app.config.get("TESTING"):
            return f(*args, **kwargs)

        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        url = f"{scheme}://{request.host}{request.path}"
        signature = request.headers.get("X-Twilio-Signature", "")

        if not validator.validate(url, request.form, signature):
            return Response("Forbidden", status=403)

        return f(*args, **kwargs)

    return decorated


@app.route("/")
def index():
    if "phone" in session:
        return redirect(url_for("send"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    phone = normalize_phone(request.form.get("phone", ""))
    admins = [normalize_phone(n) for n in get_phone_numbers("Admins")]

    if phone not in admins:
        flash("That phone number is not authorized.")
        return render_template("login.html")

    code = f"{random.randint(0, 999999):06d}"
    pending_otps[phone] = {"code": code, "expires": time.time() + 300}

    twilio_client.messages.create(
        body=f"Your Meeting SMS sign-in code is: {code}",
        from_=TWILIO_FROM,
        to=phone,
    )

    session["pending_phone"] = phone
    return redirect(url_for("verify"))


@app.route("/verify", methods=["GET", "POST"])
def verify():
    if "pending_phone" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("verify.html")

    phone = session["pending_phone"]
    code = request.form.get("code", "").strip()

    otp = pending_otps.get(phone)
    if not otp or otp["code"] != code or time.time() > otp["expires"]:
        flash("Invalid or expired code. Please try again.")
        return render_template("verify.html")

    del pending_otps[phone]
    session.pop("pending_phone", None)
    session["phone"] = phone
    session.permanent = True
    return redirect(url_for("send"))


@app.route("/send", methods=["GET", "POST"])
@login_required
def send():
    if request.method == "GET":
        return render_template("send.html", max_length=MAX_MESSAGE_LENGTH)

    message = request.form.get("message", "").strip()
    mode = request.form.get("mode", "test")

    if not message:
        flash("Please enter a message.")
        return render_template("send.html", max_length=MAX_MESSAGE_LENGTH)

    if len(message) > MAX_MESSAGE_LENGTH:
        flash(f"Message is too long ({len(message)}/{MAX_MESSAGE_LENGTH} characters).")
        return render_template("send.html", max_length=MAX_MESSAGE_LENGTH)

    full_message = message + STOP_SUFFIX

    sheet_name = "Recipients" if mode == "real" else "Test"
    contacts = get_contacts(sheet_name)
    sms_contacts = [c for c in contacts if not c["voice"] and not c["opted_out"]]

    sent_count = 0
    errors = []
    for contact in sms_contacts:
        try:
            body = full_message.replace("$NAME", contact["name"])
            twilio_client.messages.create(
                body=body, from_=TWILIO_FROM, to=contact["phone"]
            )
            sent_count += 1
        except Exception as e:
            errors.append(f"{contact['phone']}: {e}")

    _log_outgoing("SMS", mode, sent_count, message)

    return render_template(
        "sent.html",
        sent_count=sent_count,
        total=len(sms_contacts),
        errors=errors,
        mode=mode,
        message=message,
    )


APP_URL = os.environ.get("APP_URL", "https://ammsms.fly.dev")


@app.route("/voice", methods=["GET", "POST"])
@login_required
def voice():
    if request.method == "GET":
        return render_template("voice.html")

    message = request.form.get("message", "").strip()
    mode = request.form.get("mode", "test")

    if not message:
        flash("Please enter a message.")
        return render_template("voice.html")

    sheet_name = "Recipients" if mode == "real" else "Test"
    contacts = get_contacts(sheet_name)
    voice_contacts = [c for c in contacts if c["voice"] and not c["opted_out"]]

    sent_count = 0
    errors = []
    for contact in voice_contacts:
        try:
            personalized = message.replace("$NAME", contact["name"])
            twiml = VoiceResponse()
            twiml.say(
                f"This is a message from Alexandria Friends Meeting. {personalized}"
            )
            gather = twiml.gather(
                num_digits=1,
                action=f"{APP_URL}/voice-optout",
                method="POST",
            )
            gather.say("To unsubscribe from future calls, press 9.")
            twiml.say("Goodbye.")
            twilio_client.calls.create(
                to=contact["phone"],
                from_=TWILIO_FROM,
                twiml=str(twiml),
            )
            sent_count += 1
        except Exception as e:
            errors.append(f"{contact['phone']}: {e}")

    _log_outgoing("Voice", mode, sent_count, message)

    return render_template(
        "voice_sent.html",
        sent_count=sent_count,
        total=len(voice_contacts),
        errors=errors,
        mode=mode,
        message=message,
    )


@app.route("/voice-optout", methods=["POST"])
@validate_twilio_request
def voice_optout():
    digit = request.values.get("Digits", "")
    phone = request.values.get("To", "")
    resp = VoiceResponse()

    if digit != "9":
        resp.say("Goodbye.")
        return Response(str(resp), content_type="text/xml")

    # Mark the contact as opted out in the Recipients sheet
    try:
        sheet = get_sheet()
        ws = sheet.worksheet("Recipients")
        rows = ws.get_all_values()
        normalized = normalize_phone(phone)
        found_row = None
        for i, row in enumerate(rows[2:], start=3):  # 1-indexed, skip rows 1-2
            if len(row) > 1 and normalize_phone(row[1].strip()) == normalized:
                found_row = i
                break

        today = date.today().isoformat()
        if found_row:
            ws.update_cell(found_row, 4, "TRUE")  # Column D: Opted Out
            ws.update_cell(found_row, 5, today)  # Column E: Opt-Out Date
        else:
            ws.append_row(["", normalized, "TRUE", "TRUE", today])
    except Exception:
        pass  # Best-effort; don't fail the TwiML response

    resp.say("You have been unsubscribed from future calls. Goodbye.")
    return Response(str(resp), content_type="text/xml")


def _log_outgoing(msg_type, mode, sent_count, message):
    """Log an outgoing SMS or Voice broadcast to the Message Log tab."""
    from datetime import datetime

    try:
        sheet = get_sheet()
        log_ws = sheet.worksheet("Message Log")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_ws.append_row([timestamp, mode, msg_type, sent_count, message])
    except Exception:
        pass  # Best-effort; don't break the send flow


def _should_notify(sheet):
    """Check if enough time has passed since the last incoming message to send a notification.

    Returns True if there are no recent messages (within 1 hour) in SMS Replies or Voicemails.
    """
    from datetime import datetime, timedelta

    one_hour_ago = datetime.now() - timedelta(hours=1)

    for tab_name in ["SMS Replies", "Voicemails"]:
        try:
            ws = sheet.worksheet(tab_name)
            rows = ws.get_all_values()
            # Timestamps are in column C (index 2) for both tabs
            for row in reversed(rows[2:]):
                if len(row) > 2 and row[2].strip():
                    try:
                        ts = datetime.strptime(row[2].strip(), "%Y-%m-%d %H:%M:%S")
                        if ts > one_hour_ago:
                            return False
                    except ValueError:
                        continue  # Malformed timestamp, skip it
                    break  # Only need to check the most recent valid timestamp per tab
        except Exception:
            continue

    return True


def _send_admin_notifications(sheet, sender_name=""):
    """Send a notification SMS to admins who have opted in."""
    try:
        if sender_name:
            body = (
                f"New message from {sender_name}. Check the Woodlawn SMS spreadsheet."
            )
        else:
            body = "New message from an unknown number. Check the Woodlawn SMS spreadsheet."

        admins_ws = sheet.worksheet("Admins")
        rows = admins_ws.get_all_values()
        for row in rows[2:]:
            if len(row) > 2 and _is_true(row[2]) and row[1].strip():
                phone = normalize_phone(row[1].strip())
                try:
                    twilio_client.messages.create(
                        body=body,
                        from_=TWILIO_FROM,
                        to=phone,
                    )
                except Exception:
                    pass  # Best-effort per admin
    except Exception:
        pass  # Don't break the webhook response


def _lookup_name(sheet, phone):
    """Look up a contact's name across Recipients, Test, and Admins tabs."""
    for tab_name in ["Recipients", "Test", "Admins"]:
        ws = sheet.worksheet(tab_name)
        rows = ws.get_all_values()
        for row in rows[2:]:
            if len(row) > 1 and normalize_phone(row[1].strip()) == phone:
                return row[0].strip()
    return ""


@app.route("/sms-reply", methods=["POST"])
@validate_twilio_request
def sms_reply():
    from datetime import datetime

    from twilio.twiml.messaging_response import MessagingResponse

    phone = normalize_phone(request.values.get("From", ""))
    body = request.values.get("Body", "")

    try:
        sheet = get_sheet()
        should_notify = _should_notify(sheet)
        name = _lookup_name(sheet, phone)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        replies_ws = sheet.worksheet("SMS Replies")
        replies_ws.append_row([phone, name, timestamp, body])
        if should_notify:
            _send_admin_notifications(sheet, name)
    except Exception:
        pass  # Best-effort; don't fail the TwiML response

    resp = MessagingResponse()
    return Response(str(resp), content_type="text/xml")


@app.route("/incoming-call", methods=["POST"])
@validate_twilio_request
def incoming_call():
    resp = VoiceResponse()
    resp.say(
        "Hello, you've reached Alexandria Friends Meeting. "
        "No one is available to take your call. "
        "Please leave a message after the beep."
    )
    resp.record(
        max_length=120,
        timeout=5,
        transcribe=True,
        transcribe_callback=f"{APP_URL}/transcription",
        action=f"{APP_URL}/recording-complete",
        play_beep=True,
    )
    resp.say("We did not receive a recording. Goodbye.")
    return Response(str(resp), content_type="text/xml")


@app.route("/recording-complete", methods=["POST"])
@validate_twilio_request
def recording_complete():
    from datetime import datetime

    phone = normalize_phone(request.values.get("From", ""))
    recording_sid = request.values.get("RecordingSid", "")
    duration = request.values.get("RecordingDuration", "")
    proxy_url = f"{APP_URL}/recording/{recording_sid}"

    try:
        sheet = get_sheet()
        should_notify = _should_notify(sheet)
        name = _lookup_name(sheet, phone)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        voicemails_ws = sheet.worksheet("Voicemails")
        voicemails_ws.append_row([phone, name, timestamp, duration, proxy_url, ""])
        if should_notify:
            _send_admin_notifications(sheet, name)
    except Exception:
        pass

    resp = VoiceResponse()
    resp.say("Thank you for your message. Goodbye.")
    resp.hangup()
    return Response(str(resp), content_type="text/xml")


@app.route("/transcription", methods=["POST"])
@validate_twilio_request
def transcription():
    text = request.values.get("TranscriptionText", "")
    status = request.values.get("TranscriptionStatus", "")
    recording_sid = request.values.get("RecordingSid", "")

    try:
        sheet = get_sheet()
        voicemails_ws = sheet.worksheet("Voicemails")
        rows = voicemails_ws.get_all_values()

        # Find the row matching this recording SID (embedded in the proxy URL)
        found_row = None
        for i, row in enumerate(rows[2:], start=3):
            if len(row) > 4 and recording_sid in row[4]:
                found_row = i
                break

        if found_row:
            if status == "completed" and text:
                voicemails_ws.update_cell(found_row, 6, text)
            else:
                voicemails_ws.update_cell(found_row, 6, "[Transcription failed]")
    except Exception:
        pass

    return Response("", status=200)


RECORDING_SID_PATTERN = re.compile(r"^RE[0-9a-f]{32}$")
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]


@app.route("/recording/<sid>")
@login_required
def recording_proxy(sid):
    if not RECORDING_SID_PATTERN.match(sid):
        return Response("Not found", status=404)

    twilio_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Recordings/{sid}.mp3"
    resp = http_requests.get(twilio_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))

    if resp.status_code != 200:
        return Response("Not found", status=404)

    return Response(
        resp.content,
        content_type=resp.headers.get("Content-Type", "audio/mpeg"),
    )


@app.route("/guestbook.pdf")
def guestbook():
    return send_from_directory("static", "guestbook.pdf")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))
