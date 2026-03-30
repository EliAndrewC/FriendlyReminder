"""Tests for the TTS voice broadcast worker."""

import os
import signal
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars before importing worker
os.environ.setdefault(
    "GOOGLE_CREDENTIALS", '{"type":"service_account","project_id":"fake"}'
)
os.environ.setdefault("SPREADSHEET_ID", "fake-spreadsheet-id")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_fake_sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "fake_auth_token")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+18005550000")
os.environ.setdefault("MESSAGE_TEXT", "Test message")
os.environ.setdefault("MODE", "test")
os.environ.setdefault("SHEET_NAME", "Test")
os.environ.setdefault("REF_TEXT", "This is a reference text.")

import worker

# ---------------------------------------------------------------------------
# normalize_phone
# ---------------------------------------------------------------------------


class TestNormalizePhone:
    def test_ten_digits(self):
        assert worker.normalize_phone("5551234567") == "+15551234567"

    def test_with_formatting(self):
        assert worker.normalize_phone("(555) 123-4567") == "+15551234567"

    def test_already_e164(self):
        assert worker.normalize_phone("+15551234567") == "+15551234567"


# ---------------------------------------------------------------------------
# get_contacts
# ---------------------------------------------------------------------------


class TestGetContacts:
    def test_filters_voice_contacts(self):
        mock_sheet = MagicMock()
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ["Test", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "+15551111111", "TRUE", "FALSE", ""],
            ["Bob", "+15552222222", "FALSE", "FALSE", ""],
            ["Carol", "+15553333333", "TRUE", "TRUE", ""],  # opted out
        ]
        mock_sheet.worksheet.return_value = mock_ws
        contacts = worker.get_contacts(mock_sheet, "Test")
        assert len(contacts) == 1
        assert contacts[0]["name"] == "Alice"
        assert contacts[0]["phone"] == "+15551111111"

    def test_skips_empty_rows(self):
        mock_sheet = MagicMock()
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ["Test", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "+15551111111", "TRUE", "FALSE", ""],
            ["", "", "", "", ""],
            ["Bob", "+15552222222", "TRUE", "FALSE", ""],
        ]
        mock_sheet.worksheet.return_value = mock_ws
        contacts = worker.get_contacts(mock_sheet, "Test")
        assert len(contacts) == 2


# ---------------------------------------------------------------------------
# place_call (with <Play>)
# ---------------------------------------------------------------------------


class TestPlaceCall:
    def test_twiml_contains_play_verb(self):
        mock_client = MagicMock()
        worker.place_call(mock_client, "+15551111111", "https://example.com/audio.wav")
        twiml_str = mock_client.calls.create.call_args[1]["twiml"]
        assert "<Play>" in twiml_str
        assert "https://example.com/audio.wav" in twiml_str

    def test_twiml_contains_gather_optout(self):
        mock_client = MagicMock()
        worker.place_call(mock_client, "+15551111111", "https://example.com/audio.wav")
        twiml_str = mock_client.calls.create.call_args[1]["twiml"]
        assert "<Gather" in twiml_str
        assert "voice-optout" in twiml_str
        assert "9" in twiml_str

    def test_calls_correct_number(self):
        mock_client = MagicMock()
        worker.place_call(mock_client, "+15551111111", "https://example.com/audio.wav")
        assert mock_client.calls.create.call_args[1]["to"] == "+15551111111"


# ---------------------------------------------------------------------------
# place_call_with_say (fallback)
# ---------------------------------------------------------------------------


class TestPlaceCallWithSay:
    def test_twiml_contains_say_verb(self):
        mock_client = MagicMock()
        worker.place_call_with_say(mock_client, "+15551111111", "Snow day")
        twiml_str = mock_client.calls.create.call_args[1]["twiml"]
        assert "<Say>" in twiml_str
        assert "Snow day" in twiml_str
        assert "Alexandria Friends Meeting" in twiml_str

    def test_twiml_contains_gather_optout(self):
        mock_client = MagicMock()
        worker.place_call_with_say(mock_client, "+15551111111", "Hello")
        twiml_str = mock_client.calls.create.call_args[1]["twiml"]
        assert "<Gather" in twiml_str


# ---------------------------------------------------------------------------
# upload_audio
# ---------------------------------------------------------------------------


class TestUploadAudio:
    @patch.object(worker, "http_requests")
    def test_uploads_to_flask_app(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "url": "https://ammsms.fly.dev/tts-audio/test.wav"
        }
        mock_requests.post.return_value = mock_resp

        # Create a temp file to upload
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"fake audio")
            tmp = f.name

        url = worker.upload_audio(tmp, "test.wav")
        assert "tts-audio/test.wav" in url
        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args[1]
        assert "X-Upload-Secret" in call_kwargs["headers"]

        os.unlink(tmp)


# ---------------------------------------------------------------------------
# generate_audio
# ---------------------------------------------------------------------------


class TestGenerateAudio:
    @patch("worker.F5TTS", create=True)
    def test_calls_f5tts_with_correct_params(self, mock_f5tts_class):
        # Need to mock the import inside generate_audio
        mock_tts = MagicMock()
        mock_f5tts_class.return_value = mock_tts

        with patch.dict(
            "sys.modules",
            {"f5_tts": MagicMock(), "f5_tts.api": MagicMock(F5TTS=mock_f5tts_class)},
        ):
            worker.generate_audio("Hello world", "/tmp/output.wav")

        mock_tts.infer.assert_called_once()
        call_kwargs = mock_tts.infer.call_args[1]
        assert call_kwargs["gen_text"] == "Hello world"
        assert call_kwargs["file_wave"] == "/tmp/output.wav"


# ---------------------------------------------------------------------------
# log_outgoing
# ---------------------------------------------------------------------------


class TestLogOutgoing:
    def test_appends_row_to_message_log(self):
        mock_sheet = MagicMock()
        mock_log_ws = MagicMock()
        mock_sheet.worksheet.return_value = mock_log_ws

        worker.log_outgoing(mock_sheet, "test", 5, "Hello everyone")

        mock_log_ws.insert_row.assert_called_once()
        row = mock_log_ws.insert_row.call_args[0][0]
        assert row[1] == "test"
        assert row[2] == "Voice"
        assert row[3] == 5
        assert row[4] == "Hello everyone"

    def test_handles_error_gracefully(self):
        mock_sheet = MagicMock()
        mock_sheet.worksheet.side_effect = Exception("Sheet error")
        # Should not raise
        worker.log_outgoing(mock_sheet, "test", 5, "Hello")


# ---------------------------------------------------------------------------
# main function integration
# ---------------------------------------------------------------------------


class TestMain:
    @patch.object(worker, "log_outgoing")
    @patch.object(worker, "place_call_with_say")
    @patch.object(worker, "get_contacts")
    @patch.object(worker, "gspread")
    @patch.object(worker, "get_credentials")
    @patch.object(worker, "signal")
    def test_main_with_no_tts_falls_back_to_say(
        self,
        mock_signal,
        mock_creds,
        mock_gspread,
        mock_get_contacts,
        mock_place_say,
        mock_log,
    ):
        mock_creds.return_value = MagicMock()
        mock_sheet = MagicMock()
        mock_gspread.authorize.return_value.open_by_key.return_value = mock_sheet
        mock_get_contacts.return_value = [
            {"name": "Alice", "phone": "+15551111111"},
            {"name": "Bob", "phone": "+15552222222"},
        ]

        with patch.dict(os.environ, {"MESSAGE_TEXT": "Snow day", "REF_TEXT": ""}):
            worker.main()

        assert mock_place_say.call_count == 2
        mock_log.assert_called_once()

    @patch.object(worker, "log_outgoing")
    @patch.object(worker, "get_contacts")
    @patch.object(worker, "gspread")
    @patch.object(worker, "get_credentials")
    @patch.object(worker, "signal")
    def test_main_exits_with_no_contacts(
        self, mock_signal, mock_creds, mock_gspread, mock_get_contacts, mock_log
    ):
        mock_creds.return_value = MagicMock()
        mock_sheet = MagicMock()
        mock_gspread.authorize.return_value.open_by_key.return_value = mock_sheet
        mock_get_contacts.return_value = []

        worker.main()

        mock_log.assert_not_called()

    @patch.object(worker, "log_outgoing")
    @patch.object(worker, "get_contacts")
    @patch.object(worker, "gspread")
    @patch.object(worker, "get_credentials")
    @patch.object(worker, "signal")
    def test_main_exits_with_no_message(
        self, mock_signal, mock_creds, mock_gspread, mock_get_contacts, mock_log
    ):
        with patch.dict(os.environ, {"MESSAGE_TEXT": ""}):
            worker.main()

        mock_get_contacts.assert_not_called()


class TestTimeout:
    def test_timeout_handler_exits(self):
        with pytest.raises(SystemExit):
            worker.timeout_handler(signal.SIGALRM, None)
