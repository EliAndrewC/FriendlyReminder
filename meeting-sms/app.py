import json
import os
import random
import time
from datetime import timedelta
from functools import wraps

import gspread
from flask import Flask, flash, redirect, render_template, request, send_from_directory, session, url_for
from google.oauth2.service_account import Credentials
from twilio.rest import Client

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)

twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
)
TWILIO_FROM = os.environ["TWILIO_FROM_NUMBER"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

STOP_SUFFIX = "\nReply STOP to unsubscribe"
MAX_MESSAGE_LENGTH = 160 - len(STOP_SUFFIX)

# In-memory OTP storage: {phone: {"code": str, "expires": float}}
pending_otps = {}


def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def get_phone_numbers(sheet_name):
    """Get phone numbers from a named sheet. Expects header row, numbers in column B."""
    worksheet = get_sheet().worksheet(sheet_name)
    rows = worksheet.get_all_values()
    # Skip row 1 (explanation) and row 2 (headers)
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

    if mode == "real":
        numbers = [normalize_phone(n) for n in get_phone_numbers("Recipients")]
    else:
        numbers = [normalize_phone(n) for n in get_phone_numbers("Test")]

    sent_count = 0
    errors = []
    for number in numbers:
        try:
            twilio_client.messages.create(
                body=full_message, from_=TWILIO_FROM, to=number
            )
            sent_count += 1
        except Exception as e:
            errors.append(f"{number}: {e}")

    return render_template(
        "sent.html",
        sent_count=sent_count,
        total=len(numbers),
        errors=errors,
        mode=mode,
        message=message,
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
