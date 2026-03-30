"""Generate a voicemail greeting using F5-TTS and upload to the Flask app."""

import os
import signal
import sys
import traceback
import wave


def timeout_handler(signum, frame):
    print("ERROR: Timed out", flush=True)
    sys.exit(1)


def preprocess_reference_audio(input_path, output_path):
    """Convert reference audio to a clean 24kHz mono WAV that F5-TTS can read.

    Uses soundfile to read (handles various formats) and writes a standard WAV
    that any audio loader can handle without needing special codecs.
    """
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(input_path, dtype="float32")

    # Convert to mono if stereo
    if data.ndim > 1:
        data = data.mean(axis=1)

    # Resample to 24kHz if needed (F5-TTS native rate)
    if sr != 24000:
        import soxr

        data = soxr.resample(data, sr, 24000)
        sr = 24000

    # Write as standard WAV
    sf.write(output_path, data, sr, subtype="PCM_16")
    print(f"  Preprocessed: {len(data)/sr:.1f}s at {sr}Hz", flush=True)


def main():
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(900)

    greeting_text = os.environ.get("GREETING_TEXT", "")
    ref_text = os.environ.get("REF_TEXT", "")
    app_url = os.environ.get("APP_URL", "https://ammsms.fly.dev")
    upload_secret = os.environ.get("TTS_UPLOAD_SECRET", "")

    if not greeting_text:
        print("ERROR: No GREETING_TEXT provided", flush=True)
        sys.exit(1)

    # Find reference audio
    ref_audio = "/app/ref_audio/admin_voice.wav"
    if not os.path.exists(ref_audio):
        ref_audio = "/app/ref_audio/admin_voice.mp3"
    if not os.path.exists(ref_audio):
        print("ERROR: No reference audio found", flush=True)
        sys.exit(1)

    try:
        # Pre-process reference audio to a clean WAV that any loader can handle
        print("Preprocessing reference audio...", flush=True)
        preprocessed_ref = "/tmp/ref_24k.wav"
        preprocess_reference_audio(ref_audio, preprocessed_ref)

        print("Loading F5-TTS...", flush=True)
        from f5_tts.api import F5TTS

        tts = F5TTS(device="cpu")

        print("Generating audio...", flush=True)
        tts.infer(
            ref_file=preprocessed_ref,
            ref_text=ref_text,
            gen_text=greeting_text,
            file_wave="/tmp/greeting.wav",
        )

        with wave.open("/tmp/greeting.wav", "rb") as w:
            dur = w.getnframes() / w.getframerate()
            print(f"Generated: {dur:.1f}s at {w.getframerate()}Hz", flush=True)
            if dur < 5:
                print("WARNING: Output seems too short!", flush=True)

        print("Uploading...", flush=True)
        import requests

        with open("/tmp/greeting.wav", "rb") as f:
            resp = requests.post(
                f"{app_url}/tts-upload",
                files={"audio": ("voicemail_greeting.wav", f, "audio/wav")},
                data={"filename": "voicemail_greeting.wav"},
                headers={"X-Upload-Secret": upload_secret},
                timeout=30,
            )
        resp.raise_for_status()
        print(f"Done! {resp.json()['url']}", flush=True)

    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
