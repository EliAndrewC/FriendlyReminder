"""Self-contained TTS voice broadcast worker.

Runs on a CPU/GPU machine, generates cloned-voice audio with F5-TTS,
uploads to the Flask app, places Twilio calls, and exits.

All input comes via environment variables. The process auto-destroys
when it exits (Fly.io auto_destroy=true).
"""

import json
import os
import signal
import sys
from datetime import datetime

import gspread
import requests as http_requests
from google.oauth2.service_account import Credentials
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

# --- Configuration from environment ---

MODE = os.environ.get("MODE", "test")
SHEET_NAME = os.environ.get("SHEET_NAME", "Test")
APP_URL = os.environ.get("APP_URL", "https://ammsms.fly.dev")
REF_TEXT = os.environ.get("REF_TEXT", "")
WORKER_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "900"))

TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
TTS_UPLOAD_SECRET = os.environ.get("TTS_UPLOAD_SECRET", "")
REF_AUDIO_PATH = os.path.join(os.path.dirname(__file__), "ref_audio", "admin_voice.wav")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def timeout_handler(signum, frame):
    print("ERROR: Worker timed out, exiting.")
    sys.exit(1)


def get_credentials():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    return Credentials.from_service_account_info(creds_json, scopes=SCOPES)


def get_contacts(sheet, sheet_name):
    """Read voice contacts from the spreadsheet."""
    ws = sheet.worksheet(sheet_name)
    rows = ws.get_all_values()
    contacts = []
    for row in rows[2:]:
        if len(row) < 2 or not row[1].strip():
            continue
        phone = normalize_phone(row[1].strip())
        voice = row[2].strip().upper() == "TRUE" if len(row) > 2 else False
        opted_out = row[3].strip().upper() == "TRUE" if len(row) > 3 else False
        if voice and not opted_out:
            contacts.append({"name": row[0].strip(), "phone": phone})
    return contacts


def normalize_phone(phone):
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone


def generate_audio(text, output_path):
    """Generate speech audio using F5-TTS with voice cloning."""
    # Monkey-patch torchaudio.load to use soundfile instead of torchcodec
    # (torchcodec requires CUDA/FFmpeg libs that aren't available on CPU)
    import soundfile as sf
    import torch
    import torchaudio

    def _load_with_soundfile(filepath, **kwargs):
        data, sr = sf.read(filepath, dtype="float32")
        tensor = (
            torch.from_numpy(data).unsqueeze(0)
            if data.ndim == 1
            else torch.from_numpy(data.T)
        )
        return tensor, sr

    torchaudio.load = _load_with_soundfile

    from f5_tts.api import F5TTS

    tts = F5TTS(device="cpu")
    tts.infer(
        ref_file=REF_AUDIO_PATH,
        ref_text=REF_TEXT,
        gen_text=text,
        file_wave=output_path,
    )


def upload_audio(file_path, filename):
    """Upload audio to the Flask app and return the public URL."""
    with open(file_path, "rb") as f:
        resp = http_requests.post(
            f"{APP_URL}/tts-upload",
            files={"audio": (filename, f, "audio/wav")},
            data={"filename": filename},
            headers={"X-Upload-Secret": TTS_UPLOAD_SECRET},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()["url"]


def place_call(twilio_client, to, audio_url):
    """Place a Twilio call with <Play> for the cloned audio."""
    twiml = VoiceResponse()
    twiml.play(audio_url)
    gather = twiml.gather(
        num_digits=1,
        action=f"{APP_URL}/voice-optout",
        method="POST",
    )
    gather.say("To unsubscribe from future calls, press 9.")
    twiml.say("Goodbye.")

    twilio_client.calls.create(
        to=to,
        from_=TWILIO_FROM,
        twiml=str(twiml),
    )


def place_call_with_say(twilio_client, to, text):
    """Fallback: place a call using Twilio's built-in <Say> TTS."""
    twiml = VoiceResponse()
    twiml.say(f"This is a message from Alexandria Friends Meeting. {text}")
    gather = twiml.gather(
        num_digits=1,
        action=f"{APP_URL}/voice-optout",
        method="POST",
    )
    gather.say("To unsubscribe from future calls, press 9.")
    twiml.say("Goodbye.")

    twilio_client.calls.create(
        to=to,
        from_=TWILIO_FROM,
        twiml=str(twiml),
    )


def log_outgoing(sheet, mode, sent_count, message):
    """Log the broadcast to the Message Log tab."""
    try:
        log_ws = sheet.worksheet("Message Log")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_ws.insert_row([timestamp, mode, "Voice", sent_count, message], index=3)
    except Exception as e:
        print(f"WARNING: Failed to log to spreadsheet: {e}")


def main():
    # Set timeout so the process never runs forever
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(WORKER_TIMEOUT)

    message_text = os.environ.get("MESSAGE_TEXT", "")

    print(f"Worker starting: mode={MODE}, sheet={SHEET_NAME}")

    if not message_text:
        print("ERROR: No MESSAGE_TEXT provided, exiting.")
        return

    # Initialize clients
    creds = get_credentials()
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    twilio_client = Client(
        os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    )

    # Read contacts
    contacts = get_contacts(sheet, SHEET_NAME)
    if not contacts:
        print("No voice contacts found, exiting.")
        return

    print(f"Found {len(contacts)} voice contacts")

    # Check if we can use TTS
    tts_available = os.path.exists(REF_AUDIO_PATH) and REF_TEXT
    if tts_available:
        try:
            from f5_tts.api import F5TTS  # noqa: F401

            print("F5-TTS available, will generate cloned voice audio")
        except ImportError:
            print("WARNING: F5-TTS not available, falling back to <Say>")
            tts_available = False

    # Determine if we need per-contact audio (if $NAME is used)
    needs_personalization = "$NAME" in message_text

    # Generate shared audio if no personalization needed
    shared_audio_url = None
    if tts_available and not needs_personalization:
        try:
            full_text = (
                f"This is a message from Alexandria Friends Meeting. {message_text}"
            )
            output_path = "/tmp/voice_broadcast.wav"
            print("Generating shared audio...", flush=True)
            generate_audio(full_text, output_path)
            filename = f"voice_{MODE}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
            shared_audio_url = upload_audio(output_path, filename)
            print(f"Shared audio uploaded: {shared_audio_url}")
        except Exception as e:
            print(f"WARNING: TTS generation failed, falling back to <Say>: {e}")
            tts_available = False

    # Place calls
    sent_count = 0
    for contact in contacts:
        personalized = message_text.replace("$NAME", contact["name"])
        try:
            if tts_available and shared_audio_url:
                place_call(twilio_client, contact["phone"], shared_audio_url)
            elif tts_available and needs_personalization:
                # Generate per-contact audio
                full_text = (
                    f"This is a message from Alexandria Friends Meeting. {personalized}"
                )
                output_path = f"/tmp/voice_{contact['phone']}.wav"
                generate_audio(full_text, output_path)
                filename = f"voice_{contact['phone']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
                audio_url = upload_audio(output_path, filename)
                place_call(twilio_client, contact["phone"], audio_url)
            else:
                # Fallback to <Say>
                place_call_with_say(twilio_client, contact["phone"], personalized)
            sent_count += 1
            print(f"  Called {contact['phone']} ({contact['name'] or 'unknown'})")
        except Exception as e:
            print(f"  ERROR calling {contact['phone']}: {e}")
            # Try <Say> fallback for this contact
            if tts_available:
                try:
                    place_call_with_say(twilio_client, contact["phone"], personalized)
                    sent_count += 1
                    print(f"  Fallback <Say> succeeded for {contact['phone']}")
                except Exception as e2:
                    print(f"  Fallback also failed for {contact['phone']}: {e2}")

    # Log results
    log_outgoing(sheet, MODE, sent_count, message_text)
    print(f"Done: {sent_count}/{len(contacts)} calls placed")


if __name__ == "__main__":
    main()
