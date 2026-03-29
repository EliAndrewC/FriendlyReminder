import os
import time
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars before importing the app module
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_fake_sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "fake_auth_token")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+18005550000")
os.environ.setdefault("SPREADSHEET_ID", "fake-spreadsheet-id")
os.environ.setdefault(
    "GOOGLE_CREDENTIALS", '{"type":"service_account","project_id":"fake"}'
)

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test-secret"
    app_module.pending_otps.clear()
    with app_module.app.test_client() as c:
        yield c
    app_module.pending_otps.clear()


def login(client, phone="+15551234567"):
    """Helper: set session so the user is authenticated."""
    with client.session_transaction() as s:
        s["phone"] = phone


# ---------------------------------------------------------------------------
# normalize_phone
# ---------------------------------------------------------------------------


class TestNormalizePhone:
    def test_ten_digits(self):
        assert app_module.normalize_phone("5551234567") == "+15551234567"

    def test_ten_digits_with_formatting(self):
        assert app_module.normalize_phone("(555) 123-4567") == "+15551234567"

    def test_eleven_digits_starting_with_1(self):
        assert app_module.normalize_phone("15551234567") == "+15551234567"

    def test_eleven_digits_with_plus(self):
        assert app_module.normalize_phone("+15551234567") == "+15551234567"

    def test_already_e164(self):
        assert app_module.normalize_phone("+15551234567") == "+15551234567"

    def test_short_number_returned_as_is(self):
        assert app_module.normalize_phone("12345") == "12345"

    def test_non_us_number_returned_as_is(self):
        assert app_module.normalize_phone("+4412345678901") == "+4412345678901"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class TestIndex:
    def test_redirects_to_login_when_unauthenticated(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_redirects_to_send_when_authenticated(self, client):
        login(client)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/send" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_get_returns_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"Sign in" in resp.data

    @patch.object(app_module, "get_phone_numbers", return_value=["+15551234567"])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_post_valid_admin_sends_otp(self, mock_sms, mock_numbers, client):
        resp = client.post("/login", data={"phone": "(555) 123-4567"})
        assert resp.status_code == 302
        assert "/verify" in resp.headers["Location"]
        mock_sms.assert_called_once()
        call_kwargs = mock_sms.call_args[1]
        assert call_kwargs["to"] == "+15551234567"
        assert "sign-in code" in call_kwargs["body"]
        # OTP should be stored
        assert "+15551234567" in app_module.pending_otps

    @patch.object(app_module, "get_phone_numbers", return_value=["+15559999999"])
    def test_post_unauthorized_number_flashes_error(self, mock_numbers, client):
        resp = client.post("/login", data={"phone": "(555) 123-4567"})
        assert resp.status_code == 200
        assert b"not authorized" in resp.data

    @patch.object(app_module, "get_phone_numbers", return_value=["+15551234567"])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_otp_code_is_six_digits(self, mock_sms, mock_numbers, client):
        client.post("/login", data={"phone": "5551234567"})
        otp = app_module.pending_otps["+15551234567"]
        assert len(otp["code"]) == 6
        assert otp["code"].isdigit()

    @patch.object(app_module, "get_phone_numbers", return_value=["+15551234567"])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_otp_expires_in_five_minutes(self, mock_sms, mock_numbers, client):
        before = time.time()
        client.post("/login", data={"phone": "5551234567"})
        otp = app_module.pending_otps["+15551234567"]
        # Expiry should be ~300 seconds from now
        assert 299 <= otp["expires"] - before <= 301


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_get_without_pending_phone_redirects_to_login(self, client):
        resp = client.get("/verify")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_get_with_pending_phone_shows_form(self, client):
        with client.session_transaction() as s:
            s["pending_phone"] = "+15551234567"
        resp = client.get("/verify")
        assert resp.status_code == 200
        assert b"Enter your code" in resp.data

    def test_correct_code_logs_in(self, client):
        phone = "+15551234567"
        app_module.pending_otps[phone] = {
            "code": "123456",
            "expires": time.time() + 300,
        }
        with client.session_transaction() as s:
            s["pending_phone"] = phone
        resp = client.post("/verify", data={"code": "123456"})
        assert resp.status_code == 302
        assert "/send" in resp.headers["Location"]
        with client.session_transaction() as s:
            assert s["phone"] == phone
            assert "pending_phone" not in s
        # OTP should be consumed
        assert phone not in app_module.pending_otps

    def test_wrong_code_flashes_error(self, client):
        phone = "+15551234567"
        app_module.pending_otps[phone] = {
            "code": "123456",
            "expires": time.time() + 300,
        }
        with client.session_transaction() as s:
            s["pending_phone"] = phone
        resp = client.post("/verify", data={"code": "000000"})
        assert resp.status_code == 200
        assert b"Invalid or expired" in resp.data

    def test_expired_code_flashes_error(self, client):
        phone = "+15551234567"
        app_module.pending_otps[phone] = {"code": "123456", "expires": time.time() - 1}
        with client.session_transaction() as s:
            s["pending_phone"] = phone
        resp = client.post("/verify", data={"code": "123456"})
        assert resp.status_code == 200
        assert b"Invalid or expired" in resp.data

    def test_no_otp_on_file_flashes_error(self, client):
        with client.session_transaction() as s:
            s["pending_phone"] = "+15551234567"
        resp = client.post("/verify", data={"code": "123456"})
        assert resp.status_code == 200
        assert b"Invalid or expired" in resp.data


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    def test_requires_login(self, client):
        resp = client.get("/send")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_get_shows_form(self, client):
        login(client)
        resp = client.get("/send")
        assert resp.status_code == 200
        assert b"Send a message" in resp.data
        assert str(app_module.MAX_MESSAGE_LENGTH).encode() in resp.data

    def test_empty_message_flashes_error(self, client):
        login(client)
        resp = client.post("/send", data={"message": "  ", "mode": "test"})
        assert resp.status_code == 200
        assert b"Please enter a message" in resp.data

    def test_too_long_message_flashes_error(self, client):
        login(client)
        long_msg = "x" * (app_module.MAX_MESSAGE_LENGTH + 1)
        resp = client.post("/send", data={"message": long_msg, "mode": "test"})
        assert resp.status_code == 200
        assert b"too long" in resp.data

    @patch.object(
        app_module,
        "get_contacts",
        return_value=[
            {"phone": "+15559990001", "voice": False, "opted_out": False, "name": "A"},
            {"phone": "+15559990002", "voice": False, "opted_out": False, "name": "B"},
        ],
    )
    @patch.object(app_module.twilio_client.messages, "create")
    def test_send_test_mode(self, mock_sms, mock_contacts, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        mock_contacts.assert_called_once_with("Test")
        assert mock_sms.call_count == 2
        # Verify STOP suffix appended
        sent_body = mock_sms.call_args_list[0][1]["body"]
        assert sent_body == "Hello\nReply STOP to unsubscribe"

    @patch.object(
        app_module,
        "get_contacts",
        return_value=[
            {"phone": "+15559990001", "voice": False, "opted_out": False, "name": "A"},
        ],
    )
    @patch.object(app_module.twilio_client.messages, "create")
    def test_send_real_mode(self, mock_sms, mock_contacts, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "real"})
        assert resp.status_code == 200
        mock_contacts.assert_called_once_with("Recipients")
        assert mock_sms.call_count == 1

    @patch.object(
        app_module,
        "get_contacts",
        return_value=[
            {"phone": "+15559990001", "voice": False, "opted_out": False, "name": "A"},
            {"phone": "+15559990002", "voice": False, "opted_out": False, "name": "B"},
            {"phone": "+15559990003", "voice": False, "opted_out": False, "name": "C"},
        ],
    )
    @patch.object(app_module.twilio_client.messages, "create")
    def test_sent_page_shows_count(self, mock_sms, mock_contacts, client):
        login(client)
        resp = client.post("/send", data={"message": "Hi", "mode": "test"})
        assert b"3" in resp.data  # sent_count and total
        assert b"Test messages sent" in resp.data

    @patch.object(
        app_module,
        "get_contacts",
        return_value=[
            {"phone": "+15559990001", "voice": False, "opted_out": False, "name": "A"},
        ],
    )
    @patch.object(app_module.twilio_client.messages, "create")
    def test_sent_page_real_mode_heading(self, mock_sms, mock_contacts, client):
        login(client)
        resp = client.post("/send", data={"message": "Hi", "mode": "real"})
        assert b"Messages sent" in resp.data

    @patch.object(
        app_module,
        "get_contacts",
        return_value=[
            {"phone": "+15559990001", "voice": False, "opted_out": False, "name": "A"},
            {"phone": "+15559990002", "voice": False, "opted_out": False, "name": "B"},
        ],
    )
    @patch.object(
        app_module.twilio_client.messages,
        "create",
        side_effect=[None, Exception("Twilio error")],
    )
    def test_send_with_partial_failure(self, mock_sms, mock_contacts, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        assert b"1" in resp.data  # 1 of 2 sent
        assert b"Twilio error" in resp.data

    @patch.object(app_module, "get_contacts", return_value=[])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_send_to_empty_list(self, mock_sms, mock_contacts, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        mock_sms.assert_not_called()
        assert b"0" in resp.data

    def test_max_length_message_accepted(self, client):
        """A message exactly at the limit should be sent, not rejected."""
        login(client)
        msg = "x" * app_module.MAX_MESSAGE_LENGTH
        with patch.object(
            app_module,
            "get_contacts",
            return_value=[
                {
                    "phone": "+15559990001",
                    "voice": False,
                    "opted_out": False,
                    "name": "A",
                },
            ],
        ), patch.object(app_module.twilio_client.messages, "create"):
            resp = client.post("/send", data={"message": msg, "mode": "test"})
            assert resp.status_code == 200
            assert b"too long" not in resp.data


# ---------------------------------------------------------------------------
# Message length constants
# ---------------------------------------------------------------------------


class TestMessageLength:
    def test_stop_suffix_plus_max_fits_single_sms(self):
        assert app_module.MAX_MESSAGE_LENGTH + len(app_module.STOP_SUFFIX) == 160

    def test_max_length_is_positive(self):
        assert app_module.MAX_MESSAGE_LENGTH > 0


# ---------------------------------------------------------------------------
# Static / informational routes
# ---------------------------------------------------------------------------


class TestStaticRoutes:
    def test_privacy(self, client):
        resp = client.get("/privacy")
        assert resp.status_code == 200

    def test_terms(self, client):
        resp = client.get("/terms")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_session(self, client):
        login(client)
        resp = client.get("/logout")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        with client.session_transaction() as s:
            assert "phone" not in s

    def test_logout_when_not_logged_in(self, client):
        resp = client.get("/logout")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Navigation (base template)
# ---------------------------------------------------------------------------


class TestNavigation:
    def test_nav_shows_sign_out_when_logged_in(self, client):
        login(client)
        resp = client.get("/send")
        assert b"Sign out" in resp.data

    def test_nav_hides_sign_out_when_logged_out(self, client):
        resp = client.get("/login")
        assert b"Sign out" not in resp.data


# ---------------------------------------------------------------------------
# get_contacts (new structured contact reader)
# ---------------------------------------------------------------------------


class TestGetContacts:
    """Tests for get_contacts() which returns structured contact data."""

    def _mock_worksheet(self, rows):
        """Create a mock worksheet returning the given rows from get_all_values()."""
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = rows
        return mock_ws

    @patch.object(app_module, "get_sheet")
    def test_basic_sms_contact(self, mock_sheet):
        rows = [
            ["Recipients", "Phone list"],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "5551234567", "FALSE", "FALSE", ""],
        ]
        mock_sheet.return_value.worksheet.return_value = self._mock_worksheet(rows)
        contacts = app_module.get_contacts("Recipients")
        assert len(contacts) == 1
        assert contacts[0]["phone"] == "+15551234567"
        assert contacts[0]["voice"] is False
        assert contacts[0]["opted_out"] is False

    @patch.object(app_module, "get_sheet")
    def test_voice_contact(self, mock_sheet):
        rows = [
            ["Recipients", "Phone list"],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Bob", "5559876543", "TRUE", "FALSE", ""],
        ]
        mock_sheet.return_value.worksheet.return_value = self._mock_worksheet(rows)
        contacts = app_module.get_contacts("Recipients")
        assert len(contacts) == 1
        assert contacts[0]["voice"] is True

    @patch.object(app_module, "get_sheet")
    def test_opted_out_contact(self, mock_sheet):
        rows = [
            ["Recipients", "Phone list"],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Carol", "5551111111", "TRUE", "TRUE", "2026-03-28"],
        ]
        mock_sheet.return_value.worksheet.return_value = self._mock_worksheet(rows)
        contacts = app_module.get_contacts("Recipients")
        assert len(contacts) == 1
        assert contacts[0]["opted_out"] is True

    @patch.object(app_module, "get_sheet")
    def test_skips_empty_phone_rows(self, mock_sheet):
        rows = [
            ["Recipients", "Phone list"],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "5551234567", "FALSE", "FALSE", ""],
            ["", "", "", "", ""],
            ["Bob", "5559876543", "FALSE", "FALSE", ""],
        ]
        mock_sheet.return_value.worksheet.return_value = self._mock_worksheet(rows)
        contacts = app_module.get_contacts("Recipients")
        assert len(contacts) == 2

    @patch.object(app_module, "get_sheet")
    def test_missing_columns_default_to_false(self, mock_sheet):
        """Rows with only Name and Phone (legacy format) default to SMS, not opted out."""
        rows = [
            ["Recipients", "Phone list"],
            ["Name", "Phone"],
            ["Alice", "5551234567"],
        ]
        mock_sheet.return_value.worksheet.return_value = self._mock_worksheet(rows)
        contacts = app_module.get_contacts("Recipients")
        assert len(contacts) == 1
        assert contacts[0]["voice"] is False
        assert contacts[0]["opted_out"] is False

    @patch.object(app_module, "get_sheet")
    def test_checkbox_true_values(self, mock_sheet):
        """Google Sheets checkboxes can return TRUE/true/True."""
        rows = [
            ["Test", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["A", "5551111111", "TRUE", "FALSE", ""],
            ["B", "5552222222", "true", "false", ""],
            ["C", "5553333333", "True", "True", ""],
        ]
        mock_sheet.return_value.worksheet.return_value = self._mock_worksheet(rows)
        contacts = app_module.get_contacts("Test")
        assert contacts[0]["voice"] is True
        assert contacts[1]["voice"] is True
        assert contacts[2]["voice"] is True
        assert contacts[0]["opted_out"] is False
        assert contacts[1]["opted_out"] is False
        assert contacts[2]["opted_out"] is True


# ---------------------------------------------------------------------------
# SMS send with voice/opted-out filtering
# ---------------------------------------------------------------------------


class TestSendFiltering:
    """SMS send should skip voice contacts and opted-out contacts."""

    def _contacts(self, *specs):
        """Build contact dicts. specs are tuples of (phone, voice, opted_out)."""
        return [
            {"phone": p, "voice": v, "opted_out": o, "name": f"Person{i}"}
            for i, (p, v, o) in enumerate(specs)
        ]

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_skips_voice_contacts(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15551111111", False, False),
            ("+15552222222", True, False),  # voice — should be skipped
        )
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        assert mock_sms.call_count == 1
        assert mock_sms.call_args[1]["to"] == "+15551111111"

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_skips_opted_out_contacts(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15551111111", False, False),
            ("+15553333333", False, True),  # opted out — should be skipped
        )
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert mock_sms.call_count == 1

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_real_mode_uses_recipients(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15551111111", False, False),
        )
        client.post("/send", data={"message": "Hello", "mode": "real"})
        mock_contacts.assert_called_once_with("Recipients")

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_test_mode_uses_test(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15551111111", False, False),
        )
        client.post("/send", data={"message": "Hello", "mode": "test"})
        mock_contacts.assert_called_once_with("Test")


# ---------------------------------------------------------------------------
# Voice send
# ---------------------------------------------------------------------------


class TestVoiceSend:
    def _contacts(self, *specs):
        return [
            {"phone": p, "voice": v, "opted_out": o, "name": f"Person{i}"}
            for i, (p, v, o) in enumerate(specs)
        ]

    def test_requires_login(self, client):
        resp = client.get("/voice")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_get_shows_form(self, client):
        login(client)
        resp = client.get("/voice")
        assert resp.status_code == 200
        assert b"voice message" in resp.data.lower()

    def test_empty_message_flashes_error(self, client):
        login(client)
        resp = client.post("/voice", data={"message": "  ", "mode": "test"})
        assert resp.status_code == 200
        assert b"Please enter a message" in resp.data

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_sends_to_voice_contacts_only(
        self, mock_contacts, mock_calls, client
    ):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15551111111", False, False),  # SMS — should be skipped
            ("+15552222222", True, False),  # voice — should be called
        )
        resp = client.post("/voice", data={"message": "Hello everyone", "mode": "test"})
        assert resp.status_code == 200
        assert mock_calls.call_count == 1
        assert mock_calls.call_args[1]["to"] == "+15552222222"

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_skips_opted_out(self, mock_contacts, mock_calls, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15552222222", True, False),
            ("+15553333333", True, True),  # opted out
        )
        resp = client.post("/voice", data={"message": "Hello", "mode": "test"})
        assert mock_calls.call_count == 1

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_twiml_contains_message(self, mock_contacts, mock_calls, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15552222222", True, False),
        )
        client.post("/voice", data={"message": "Snow day announcement", "mode": "test"})
        twiml_str = mock_calls.call_args[1]["twiml"]
        assert "Snow day announcement" in twiml_str

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_twiml_contains_optout_gather(
        self, mock_contacts, mock_calls, client
    ):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15552222222", True, False),
        )
        client.post("/voice", data={"message": "Hello", "mode": "test"})
        twiml_str = mock_calls.call_args[1]["twiml"]
        assert "<Gather" in twiml_str
        assert "9" in twiml_str

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_real_mode_uses_recipients(self, mock_contacts, mock_calls, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15552222222", True, False),
        )
        client.post("/voice", data={"message": "Hello", "mode": "real"})
        mock_contacts.assert_called_once_with("Recipients")

    @patch.object(
        app_module.twilio_client.calls, "create", side_effect=Exception("Call failed")
    )
    @patch.object(app_module, "get_contacts")
    def test_voice_with_error(self, mock_contacts, mock_calls, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15552222222", True, False),
        )
        resp = client.post("/voice", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        assert b"Call failed" in resp.data

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_sent_page_shows_count(self, mock_contacts, mock_calls, client):
        login(client)
        mock_contacts.return_value = self._contacts(
            ("+15552222222", True, False),
            ("+15553333333", True, False),
        )
        resp = client.post("/voice", data={"message": "Hello", "mode": "test"})
        assert b"2" in resp.data


# ---------------------------------------------------------------------------
# Voice opt-out webhook
# ---------------------------------------------------------------------------


class TestVoiceOptout:
    @patch.object(app_module, "get_sheet")
    def test_optout_marks_contact(self, mock_sheet, client):
        """Pressing 9 should mark the contact as opted out and write the date."""
        mock_ws = MagicMock()
        # Simulate finding the phone in row 4 (1-indexed), column B
        mock_ws.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "+15551234567", "TRUE", "FALSE", ""],
            ["Bob", "+15559876543", "TRUE", "FALSE", ""],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_ws
        resp = client.post(
            "/voice-optout",
            data={"Digits": "9", "To": "+15559876543"},
        )
        assert resp.status_code == 200
        assert b"unsubscribed" in resp.data.lower()
        # Should update row 4 (1-indexed: row1=explanation, row2=headers, row3=Alice, row4=Bob)
        mock_ws.update_cell.assert_any_call(4, 4, "TRUE")  # Opted Out column
        # Should also write the date
        assert mock_ws.update_cell.call_count == 2

    @patch.object(app_module, "get_sheet")
    def test_optout_wrong_digit_no_action(self, mock_sheet, client):
        resp = client.post(
            "/voice-optout",
            data={"Digits": "5", "To": "+15559876543"},
        )
        assert resp.status_code == 200
        assert b"no action" in resp.data.lower() or b"Goodbye" in resp.data
        mock_sheet.return_value.worksheet.return_value.update_cell.assert_not_called()

    @patch.object(app_module, "get_sheet")
    def test_optout_creates_row_if_not_found(self, mock_sheet, client):
        """Edge case: number not in sheet should add a new row."""
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "+15551234567", "TRUE", "FALSE", ""],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_ws
        resp = client.post(
            "/voice-optout",
            data={"Digits": "9", "To": "+15559999999"},
        )
        assert resp.status_code == 200
        assert b"unsubscribed" in resp.data.lower()
        mock_ws.append_row.assert_called_once()
        appended = mock_ws.append_row.call_args[0][0]
        assert appended[1] == "+15559999999"  # phone in column B
        assert appended[3] == "TRUE"  # opted out

    @patch.object(app_module, "get_sheet")
    def test_optout_searches_recipients_sheet(self, mock_sheet, client):
        """The webhook should look in the Recipients sheet."""
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_ws
        client.post(
            "/voice-optout",
            data={"Digits": "9", "To": "+15559876543"},
        )
        mock_sheet.return_value.worksheet.assert_called_with("Recipients")


# ---------------------------------------------------------------------------
# $NAME substitution
# ---------------------------------------------------------------------------


class TestNameSubstitution:
    def _contact(self, name, phone="+15559990001", voice=False):
        return {"phone": phone, "voice": voice, "opted_out": False, "name": name}

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_substitutes_name(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = [self._contact("Eli")]
        client.post("/send", data={"message": "Hi $NAME, snow day!", "mode": "test"})
        sent_body = mock_sms.call_args[1]["body"]
        assert "Hi Eli, snow day!" in sent_body
        assert "$NAME" not in sent_body

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_substitutes_different_names(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = [
            self._contact("Alice", "+15551111111"),
            self._contact("Bob", "+15552222222"),
        ]
        client.post(
            "/send", data={"message": "Hi $NAME, meeting canceled", "mode": "test"}
        )
        bodies = [call[1]["body"] for call in mock_sms.call_args_list]
        assert "Hi Alice, meeting canceled" in bodies[0]
        assert "Hi Bob, meeting canceled" in bodies[1]

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_no_name_placeholder_unchanged(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = [self._contact("Eli")]
        client.post("/send", data={"message": "Snow day!", "mode": "test"})
        sent_body = mock_sms.call_args[1]["body"]
        assert "Snow day!" in sent_body

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_contacts")
    def test_sms_empty_name_removes_placeholder(self, mock_contacts, mock_sms, client):
        login(client)
        mock_contacts.return_value = [self._contact("")]
        client.post("/send", data={"message": "Hi $NAME, snow day!", "mode": "test"})
        sent_body = mock_sms.call_args[1]["body"]
        assert "$NAME" not in sent_body
        assert "Hi , snow day!" in sent_body

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_substitutes_name(self, mock_contacts, mock_calls, client):
        login(client)
        mock_contacts.return_value = [self._contact("Eli", voice=True)]
        client.post("/voice", data={"message": "Hi $NAME, snow day!", "mode": "test"})
        twiml_str = mock_calls.call_args[1]["twiml"]
        assert "Hi Eli, snow day!" in twiml_str
        assert "$NAME" not in twiml_str

    @patch.object(app_module.twilio_client.calls, "create")
    @patch.object(app_module, "get_contacts")
    def test_voice_different_names_get_different_twiml(
        self, mock_contacts, mock_calls, client
    ):
        login(client)
        mock_contacts.return_value = [
            self._contact("Alice", "+15551111111", voice=True),
            self._contact("Bob", "+15552222222", voice=True),
        ]
        client.post(
            "/voice", data={"message": "Hi $NAME, meeting canceled", "mode": "test"}
        )
        twiml_strs = [call[1]["twiml"] for call in mock_calls.call_args_list]
        assert "Hi Alice, meeting canceled" in twiml_strs[0]
        assert "Hi Bob, meeting canceled" in twiml_strs[1]

    def test_sms_name_in_length_check_uses_placeholder(self, client):
        """$NAME in the message should count as 5 chars for length validation,
        not the expanded name length."""
        login(client)
        # A message that fits with $NAME (5 chars) but might not with a long name
        msg = "x" * (app_module.MAX_MESSAGE_LENGTH - 5) + "$NAME"
        assert len(msg) == app_module.MAX_MESSAGE_LENGTH
        with patch.object(
            app_module, "get_contacts", return_value=[self._contact("Eli")]
        ), patch.object(app_module.twilio_client.messages, "create"):
            resp = client.post("/send", data={"message": msg, "mode": "test"})
            assert resp.status_code == 200
            assert b"too long" not in resp.data


# ---------------------------------------------------------------------------
# SMS reply webhook
# ---------------------------------------------------------------------------


class TestSmsReply:
    def _mock_sheet_with_contacts(self, mock_sheet):
        """Set up mock sheet that has Recipients and Test tabs with known contacts."""
        mock_recipients = MagicMock()
        mock_recipients.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "+15551111111", "FALSE", "FALSE", ""],
            ["Bob", "+15552222222", "TRUE", "FALSE", ""],
        ]
        mock_test = MagicMock()
        mock_test.get_all_values.return_value = [
            ["Test", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Eli", "+15714352602", "FALSE", "FALSE", ""],
        ]
        mock_replies = MagicMock()
        mock_admins = MagicMock()
        mock_admins.get_all_values.return_value = [
            ["Admins", ""],
            ["Name", "Phone"],
            ["Eli", "+15714352602"],
        ]

        def worksheet_side_effect(name):
            if name == "Recipients":
                return mock_recipients
            elif name == "Test":
                return mock_test
            elif name == "SMS Replies":
                return mock_replies
            elif name == "Admins":
                return mock_admins
            raise Exception(f"Unknown tab: {name}")

        mock_sheet.return_value.worksheet.side_effect = worksheet_side_effect
        return mock_replies

    @patch.object(app_module, "get_sheet")
    def test_reply_logged_to_spreadsheet(self, mock_sheet, client):
        mock_replies = self._mock_sheet_with_contacts(mock_sheet)
        resp = client.post(
            "/sms-reply",
            data={"From": "+15551111111", "Body": "Thanks for the update!"},
        )
        assert resp.status_code == 200
        mock_replies.append_row.assert_called_once()
        row = mock_replies.append_row.call_args[0][0]
        assert row[0] == "+15551111111"  # phone
        assert row[1] == "Alice"  # name found in Recipients
        assert row[3] == "Thanks for the update!"  # message body

    @patch.object(app_module, "get_sheet")
    def test_reply_from_unknown_number(self, mock_sheet, client):
        mock_replies = self._mock_sheet_with_contacts(mock_sheet)
        resp = client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Who is this?"},
        )
        assert resp.status_code == 200
        mock_replies.append_row.assert_called_once()
        row = mock_replies.append_row.call_args[0][0]
        assert row[0] == "+15559999999"
        assert row[1] == ""  # unknown name
        assert row[3] == "Who is this?"

    @patch.object(app_module, "get_sheet")
    def test_reply_name_found_in_test_tab(self, mock_sheet, client):
        mock_replies = self._mock_sheet_with_contacts(mock_sheet)
        resp = client.post(
            "/sms-reply",
            data={"From": "+15714352602", "Body": "Got it"},
        )
        assert resp.status_code == 200
        row = mock_replies.append_row.call_args[0][0]
        assert row[1] == "Eli"

    @patch.object(app_module, "get_sheet")
    def test_reply_includes_timestamp(self, mock_sheet, client):
        mock_replies = self._mock_sheet_with_contacts(mock_sheet)
        client.post(
            "/sms-reply",
            data={"From": "+15551111111", "Body": "Hi"},
        )
        row = mock_replies.append_row.call_args[0][0]
        # Column index 2 should be a timestamp string
        assert "2026" in row[2]  # should contain the year

    @patch.object(app_module, "get_sheet")
    def test_reply_returns_empty_twiml(self, mock_sheet, client):
        """Should return valid TwiML so Twilio doesn't retry."""
        mock_replies = self._mock_sheet_with_contacts(mock_sheet)
        resp = client.post(
            "/sms-reply",
            data={"From": "+15551111111", "Body": "Hi"},
        )
        assert resp.status_code == 200
        assert resp.content_type == "text/xml"
        assert b"<Response" in resp.data

    @patch.object(app_module, "get_sheet")
    def test_reply_handles_spreadsheet_error_gracefully(self, mock_sheet, client):
        """If the spreadsheet write fails, still return valid TwiML."""
        mock_sheet.return_value.worksheet.side_effect = Exception("API error")
        resp = client.post(
            "/sms-reply",
            data={"From": "+15551111111", "Body": "Hi"},
        )
        assert resp.status_code == 200
        assert b"<Response" in resp.data


# ---------------------------------------------------------------------------
# Twilio webhook signature validation
# ---------------------------------------------------------------------------


class TestTwilioWebhookValidation:
    @pytest.fixture(autouse=True)
    def disable_testing_mode(self, client):
        """Disable TESTING so the signature validation decorator runs."""
        app_module.app.config["TESTING"] = False
        yield
        app_module.app.config["TESTING"] = True

    def _signed_post(self, client, path, data):
        """Make a POST with a valid Twilio signature."""
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
        url = f"https://ammsms.fly.dev{path}"
        signature = validator.compute_signature(url, data)
        return client.post(
            path,
            data=data,
            headers={
                "X-Twilio-Signature": signature,
                "X-Forwarded-Proto": "https",
                "Host": "ammsms.fly.dev",
            },
        )

    @patch.object(app_module, "get_sheet")
    def test_sms_reply_rejects_unsigned_request(self, mock_sheet, client):
        resp = client.post(
            "/sms-reply",
            data={"From": "+15551111111", "Body": "Hi"},
        )
        assert resp.status_code == 403

    @patch.object(app_module, "get_sheet")
    def test_sms_reply_rejects_bad_signature(self, mock_sheet, client):
        resp = client.post(
            "/sms-reply",
            data={"From": "+15551111111", "Body": "Hi"},
            headers={"X-Twilio-Signature": "invalid"},
        )
        assert resp.status_code == 403

    @patch.object(app_module, "get_sheet")
    def test_sms_reply_accepts_valid_signature(self, mock_sheet, client):
        mock_replies = MagicMock()
        mock_sheet.return_value.worksheet.return_value = mock_replies

        def ws_side_effect(name):
            if name == "SMS Replies":
                return mock_replies
            m = MagicMock()
            m.get_all_values.return_value = [["", ""], ["", ""]]
            return m

        mock_sheet.return_value.worksheet.side_effect = ws_side_effect
        resp = self._signed_post(
            client, "/sms-reply", {"From": "+15551111111", "Body": "Hi"}
        )
        assert resp.status_code == 200

    @patch.object(app_module, "get_sheet")
    def test_voice_optout_rejects_unsigned_request(self, mock_sheet, client):
        resp = client.post(
            "/voice-optout",
            data={"Digits": "9", "To": "+15559876543"},
        )
        assert resp.status_code == 403

    @patch.object(app_module, "get_sheet")
    def test_voice_optout_accepts_valid_signature(self, mock_sheet, client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_ws
        resp = self._signed_post(
            client, "/voice-optout", {"Digits": "9", "To": "+15559876543"}
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Incoming call / voicemail
# ---------------------------------------------------------------------------


class TestIncomingCall:
    def test_returns_twiml_with_greeting(self, client):
        resp = client.post(
            "/incoming-call",
            data={"From": "+15551111111", "CallSid": "CA123"},
        )
        assert resp.status_code == 200
        assert resp.content_type == "text/xml"
        assert b"<Say" in resp.data
        assert b"Alexandria Friends Meeting" in resp.data

    def test_returns_record_verb(self, client):
        resp = client.post(
            "/incoming-call",
            data={"From": "+15551111111", "CallSid": "CA123"},
        )
        assert b"<Record" in resp.data

    def test_record_has_transcription_enabled(self, client):
        resp = client.post(
            "/incoming-call",
            data={"From": "+15551111111", "CallSid": "CA123"},
        )
        data = resp.data.decode()
        assert "transcribe" in data.lower()


class TestRecordingComplete:
    def test_returns_goodbye_twiml(self, client):
        resp = client.post(
            "/recording-complete",
            data={
                "From": "+15551111111",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "RecordingSid": "RE123",
                "RecordingDuration": "15",
            },
        )
        assert resp.status_code == 200
        assert resp.content_type == "text/xml"
        assert b"<Say" in resp.data

    @patch.object(app_module, "get_sheet")
    def test_logs_recording_to_spreadsheet(self, mock_sheet, client):
        mock_voicemails = MagicMock()

        def ws_side_effect(name):
            if name == "Voicemails":
                return mock_voicemails
            m = MagicMock()
            m.get_all_values.return_value = [["", ""], ["", ""]]
            return m

        mock_sheet.return_value.worksheet.side_effect = ws_side_effect
        client.post(
            "/recording-complete",
            data={
                "From": "+15551111111",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "RecordingSid": "RE123",
                "RecordingDuration": "15",
            },
        )
        mock_voicemails.append_row.assert_called_once()
        row = mock_voicemails.append_row.call_args[0][0]
        assert row[0] == "+15551111111"  # phone
        assert "2026" in row[2]  # timestamp
        assert "/recording/RE123" in row[4]  # proxy URL with SID
        assert row[5] == ""  # transcription not yet available

    @patch.object(app_module, "get_sheet")
    def test_looks_up_caller_name(self, mock_sheet, client):
        mock_voicemails = MagicMock()
        mock_recipients = MagicMock()
        mock_recipients.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice", "+15551111111", "FALSE", "FALSE", ""],
        ]

        def ws_side_effect(name):
            if name == "Voicemails":
                return mock_voicemails
            if name == "Recipients":
                return mock_recipients
            m = MagicMock()
            m.get_all_values.return_value = [["", ""], ["", ""]]
            return m

        mock_sheet.return_value.worksheet.side_effect = ws_side_effect
        client.post(
            "/recording-complete",
            data={
                "From": "+15551111111",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "RecordingSid": "RE123",
                "RecordingDuration": "15",
            },
        )
        row = mock_voicemails.append_row.call_args[0][0]
        assert row[1] == "Alice"


class TestTranscriptionCallback:
    @patch.object(app_module, "get_sheet")
    def test_updates_row_with_transcription(self, mock_sheet, client):
        mock_voicemails = MagicMock()
        mock_voicemails.get_all_values.return_value = [
            ["Voicemails", ""],
            ["Phone", "Name", "Date/Time", "Duration", "Recording", "Transcription"],
            [
                "+15551111111",
                "Alice",
                "2026-03-28 12:00:00",
                "15",
                "https://ammsms.fly.dev/recording/RE123",
                "",
            ],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_voicemails
        resp = client.post(
            "/transcription",
            data={
                "TranscriptionText": "Hi, the parking lot has a pothole.",
                "TranscriptionStatus": "completed",
                "RecordingSid": "RE123",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "From": "+15551111111",
            },
        )
        assert resp.status_code == 200
        mock_voicemails.update_cell.assert_called_once()
        call_args = mock_voicemails.update_cell.call_args[0]
        assert call_args[1] == 6  # column F (Transcription)
        assert call_args[2] == "Hi, the parking lot has a pothole."

    @patch.object(app_module, "get_sheet")
    def test_failed_transcription_writes_status(self, mock_sheet, client):
        mock_voicemails = MagicMock()
        mock_voicemails.get_all_values.return_value = [
            ["Voicemails", ""],
            ["Phone", "Name", "Date/Time", "Duration", "Recording", "Transcription"],
            [
                "+15551111111",
                "",
                "2026-03-28 12:00:00",
                "15",
                "https://ammsms.fly.dev/recording/RE123",
                "",
            ],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_voicemails
        resp = client.post(
            "/transcription",
            data={
                "TranscriptionText": "",
                "TranscriptionStatus": "failed",
                "RecordingSid": "RE123",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "From": "+15551111111",
            },
        )
        assert resp.status_code == 200
        mock_voicemails.update_cell.assert_called_once()
        assert "failed" in mock_voicemails.update_cell.call_args[0][2].lower()

    @patch.object(app_module, "get_sheet")
    def test_transcription_matches_by_recording_sid(self, mock_sheet, client):
        """Should find the right row by matching the recording SID in the proxy URL."""
        mock_voicemails = MagicMock()
        mock_voicemails.get_all_values.return_value = [
            ["Voicemails", ""],
            ["Phone", "Name", "Date/Time", "Duration", "Recording", "Transcription"],
            [
                "+15551111111",
                "",
                "2026-03-28 11:00:00",
                "10",
                "https://ammsms.fly.dev/recording/RE000",
                "",
            ],
            [
                "+15552222222",
                "",
                "2026-03-28 12:00:00",
                "15",
                "https://ammsms.fly.dev/recording/RE123",
                "",
            ],
        ]
        mock_sheet.return_value.worksheet.return_value = mock_voicemails
        client.post(
            "/transcription",
            data={
                "TranscriptionText": "Second caller message",
                "TranscriptionStatus": "completed",
                "RecordingSid": "RE123",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "From": "+15552222222",
            },
        )
        call_args = mock_voicemails.update_cell.call_args[0]
        assert call_args[0] == 4  # row 4 (1-indexed), the second data row

    @patch.object(app_module, "get_sheet")
    def test_handles_spreadsheet_error_gracefully(self, mock_sheet, client):
        mock_sheet.return_value.worksheet.side_effect = Exception("API error")
        resp = client.post(
            "/transcription",
            data={
                "TranscriptionText": "Hello",
                "TranscriptionStatus": "completed",
                "RecordingSid": "RE123",
                "RecordingUrl": "https://api.twilio.com/recordings/RE123",
                "From": "+15551111111",
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Recording proxy
# ---------------------------------------------------------------------------


class TestRecordingProxy:
    def test_requires_login(self, client):
        resp = client.get("/recording/RE123abc")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    @patch.object(app_module, "http_requests")
    def test_streams_audio_when_logged_in(self, mock_requests, client):
        login(client)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "audio/mpeg"}
        mock_response.content = b"fake-audio-data"
        mock_requests.get.return_value = mock_response
        resp = client.get("/recording/RE12345678901234567890123456789012")
        assert resp.status_code == 200
        assert resp.content_type == "audio/mpeg"
        assert resp.data == b"fake-audio-data"
        # Verify it called the Twilio URL with auth
        call_args = mock_requests.get.call_args
        assert "RE12345678901234567890123456789012" in call_args[0][0]
        assert call_args[1]["auth"] is not None

    @patch.object(app_module, "http_requests")
    def test_returns_404_for_missing_recording(self, mock_requests, client):
        login(client)
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_requests.get.return_value = mock_response
        resp = client.get("/recording/RE12345678901234567890123456789012")
        assert resp.status_code == 404

    def test_rejects_invalid_sid_format(self, client):
        login(client)
        resp = client.get("/recording/../../etc/passwd")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Outgoing message log
# ---------------------------------------------------------------------------


class TestMessageLog:
    def _mock_contacts_and_log(self, mock_sheet, contacts, mock_log=None):
        """Set up mock sheet with contacts and a Message Log tab."""
        if mock_log is None:
            mock_log = MagicMock()
        mock_contacts_ws = MagicMock()
        mock_contacts_ws.get_all_values.return_value = contacts

        def ws_side_effect(name):
            if name == "Message Log":
                return mock_log
            return mock_contacts_ws

        mock_sheet.return_value.worksheet.side_effect = ws_side_effect
        return mock_log

    @patch.object(app_module, "get_sheet")
    @patch.object(app_module, "get_contacts")
    @patch.object(app_module.twilio_client.messages, "create")
    def test_sms_send_logs_to_spreadsheet(
        self, mock_sms, mock_contacts, mock_sheet, client
    ):
        login(client)
        mock_log = MagicMock()
        mock_sheet.return_value.worksheet.return_value = mock_log
        mock_contacts.return_value = [
            {"phone": "+15551111111", "voice": False, "opted_out": False, "name": "A"},
            {"phone": "+15552222222", "voice": False, "opted_out": False, "name": "B"},
        ]
        client.post("/send", data={"message": "Snow day!", "mode": "test"})
        mock_log.append_row.assert_called_once()
        row = mock_log.append_row.call_args[0][0]
        assert "2026" in row[0]  # timestamp
        assert row[1] == "test"  # mode
        assert row[2] == "SMS"  # type
        assert row[3] == 2  # recipients
        assert row[4] == "Snow day!"  # message

    @patch.object(app_module, "get_sheet")
    @patch.object(app_module, "get_contacts")
    @patch.object(app_module.twilio_client.messages, "create")
    def test_sms_real_mode_logged(self, mock_sms, mock_contacts, mock_sheet, client):
        login(client)
        mock_log = MagicMock()
        mock_sheet.return_value.worksheet.return_value = mock_log
        mock_contacts.return_value = [
            {"phone": "+15551111111", "voice": False, "opted_out": False, "name": "A"},
        ]
        client.post("/send", data={"message": "Hello", "mode": "real"})
        row = mock_log.append_row.call_args[0][0]
        assert row[1] == "real"

    @patch.object(app_module, "get_sheet")
    @patch.object(app_module, "get_contacts")
    @patch.object(app_module.twilio_client.calls, "create")
    def test_voice_send_logs_to_spreadsheet(
        self, mock_calls, mock_contacts, mock_sheet, client
    ):
        login(client)
        mock_log = MagicMock()
        mock_sheet.return_value.worksheet.return_value = mock_log
        mock_contacts.return_value = [
            {"phone": "+15552222222", "voice": True, "opted_out": False, "name": "A"},
            {"phone": "+15553333333", "voice": True, "opted_out": False, "name": "B"},
            {"phone": "+15554444444", "voice": True, "opted_out": False, "name": "C"},
        ]
        client.post("/voice", data={"message": "Meeting canceled", "mode": "test"})
        mock_log.append_row.assert_called_once()
        row = mock_log.append_row.call_args[0][0]
        assert "2026" in row[0]  # timestamp
        assert row[1] == "test"  # mode
        assert row[2] == "Voice"  # type
        assert row[3] == 3  # recipients
        assert row[4] == "Meeting canceled"  # message

    @patch.object(app_module, "get_sheet")
    @patch.object(app_module, "get_contacts")
    @patch.object(app_module.twilio_client.messages, "create")
    def test_log_failure_does_not_break_send(
        self, mock_sms, mock_contacts, mock_sheet, client
    ):
        """If logging fails, the send should still succeed."""
        login(client)
        mock_sheet.return_value.worksheet.side_effect = Exception("Sheet error")
        mock_contacts.return_value = [
            {"phone": "+15551111111", "voice": False, "opted_out": False, "name": "A"},
        ]
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        assert b"1" in resp.data  # still sent


# ---------------------------------------------------------------------------
# Reply notifications
# ---------------------------------------------------------------------------


class TestReplyNotifications:
    def _make_sheet_mocks(
        self,
        mock_sheet,
        *,
        admins_rows=None,
        sms_replies_rows=None,
        voicemails_rows=None,
    ):
        """Build mock worksheets for the notification logic."""
        if admins_rows is None:
            admins_rows = [
                ["Admins", ""],
                ["Name", "Phone", "Notify about replies"],
                ["Eli", "+15714352602", "TRUE"],
            ]
        if sms_replies_rows is None:
            sms_replies_rows = [
                ["SMS Replies", ""],
                ["Phone", "Name", "Date/Time", "Message"],
            ]
        if voicemails_rows is None:
            voicemails_rows = [
                ["Voicemails", ""],
                [
                    "Phone",
                    "Name",
                    "Date/Time",
                    "Duration",
                    "Recording",
                    "Transcription",
                ],
            ]

        mock_admins = MagicMock()
        mock_admins.get_all_values.return_value = admins_rows
        mock_sms_replies = MagicMock()
        mock_sms_replies.get_all_values.return_value = sms_replies_rows
        mock_voicemails = MagicMock()
        mock_voicemails.get_all_values.return_value = voicemails_rows
        mock_message_log = MagicMock()

        # Track recipients and test tabs for _lookup_name
        mock_recipients = MagicMock()
        mock_recipients.get_all_values.return_value = [["", ""], ["", ""]]
        mock_test = MagicMock()
        mock_test.get_all_values.return_value = [["", ""], ["", ""]]

        def ws_side_effect(name):
            return {
                "Admins": mock_admins,
                "SMS Replies": mock_sms_replies,
                "Voicemails": mock_voicemails,
                "Message Log": mock_message_log,
                "Recipients": mock_recipients,
                "Test": mock_test,
            }[name]

        mock_sheet.return_value.worksheet.side_effect = ws_side_effect
        return mock_sms_replies

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_notification_sent_on_sms_reply(self, mock_sheet, mock_sms, client):
        """An SMS reply should trigger a notification to opted-in admins."""
        self._make_sheet_mocks(mock_sheet)
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        # Should have been called: the notification SMS
        notification_calls = [
            c
            for c in mock_sms.call_args_list
            if "+15714352602" in str(c) and "check" in str(c).lower()
        ]
        assert len(notification_calls) == 1
        # Unknown number should say "unknown"
        assert "unknown" in notification_calls[0][1]["body"].lower()

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_notification_includes_sender_name(self, mock_sheet, mock_sms, client):
        """When the sender is known, the notification should include their name."""
        self._make_sheet_mocks(
            mock_sheet,
            # Add the sender to Recipients so _lookup_name finds them
            admins_rows=[
                ["Admins", ""],
                ["Name", "Phone", "Notify about replies"],
                ["Eli", "+15714352602", "TRUE"],
            ],
        )
        # Override Recipients to include the sender
        mock_recipients = MagicMock()
        mock_recipients.get_all_values.return_value = [
            ["Recipients", ""],
            ["Name", "Phone", "Voice", "Opted Out", "Opt-Out Date"],
            ["Alice Smith", "+15559999999", "FALSE", "FALSE", ""],
        ]
        orig_side_effect = mock_sheet.return_value.worksheet.side_effect

        def patched(name):
            if name == "Recipients":
                return mock_recipients
            return orig_side_effect(name)

        mock_sheet.return_value.worksheet.side_effect = patched
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c
            for c in mock_sms.call_args_list
            if "+15714352602" in str(c) and "check" in str(c).lower()
        ]
        assert len(notification_calls) == 1
        assert "Alice Smith" in notification_calls[0][1]["body"]

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_no_notification_when_no_admins_opted_in(
        self, mock_sheet, mock_sms, client
    ):
        self._make_sheet_mocks(
            mock_sheet,
            admins_rows=[
                ["Admins", ""],
                ["Name", "Phone", "Notify about replies"],
                ["Eli", "+15714352602", "FALSE"],
            ],
        )
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 0

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_no_notification_within_one_hour(self, mock_sheet, mock_sms, client):
        """If a recent message exists (< 1 hour ago), no notification."""
        from datetime import datetime

        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._make_sheet_mocks(
            mock_sheet,
            sms_replies_rows=[
                ["SMS Replies", ""],
                ["Phone", "Name", "Date/Time", "Message"],
                ["+15558888888", "Someone", recent, "Earlier message"],
            ],
        )
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 0

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_notification_sent_after_one_hour(self, mock_sheet, mock_sms, client):
        """If the most recent message is > 1 hour ago, send notification."""
        self._make_sheet_mocks(
            mock_sheet,
            sms_replies_rows=[
                ["SMS Replies", ""],
                ["Phone", "Name", "Date/Time", "Message"],
                ["+15558888888", "Someone", "2026-01-01 01:00:00", "Old message"],
            ],
        )
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 1

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_malformed_timestamp_treated_as_old(self, mock_sheet, mock_sms, client):
        """A malformed timestamp should not crash, and should allow notification."""
        self._make_sheet_mocks(
            mock_sheet,
            sms_replies_rows=[
                ["SMS Replies", ""],
                ["Phone", "Name", "Date/Time", "Message"],
                ["+15558888888", "Someone", "not-a-date", "Bad timestamp"],
            ],
        )
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 1

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_voicemail_recent_suppresses_sms_notification(
        self, mock_sheet, mock_sms, client
    ):
        """A recent voicemail should count toward the 1-hour window."""
        from datetime import datetime

        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._make_sheet_mocks(
            mock_sheet,
            voicemails_rows=[
                ["Voicemails", ""],
                [
                    "Phone",
                    "Name",
                    "Date/Time",
                    "Duration",
                    "Recording",
                    "Transcription",
                ],
                ["+15558888888", "", recent, "10", "https://...", ""],
            ],
        )
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 0

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_notification_on_voicemail(self, mock_sheet, mock_sms, client):
        """A voicemail should also trigger notification."""
        self._make_sheet_mocks(mock_sheet)
        client.post(
            "/recording-complete",
            data={
                "From": "+15559999999",
                "RecordingSid": "RE123",
                "RecordingDuration": "10",
            },
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 1

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_admins_without_notify_column_ignored(self, mock_sheet, mock_sms, client):
        """Admins tab with only Name/Phone columns (no Notify) should not crash."""
        self._make_sheet_mocks(
            mock_sheet,
            admins_rows=[
                ["Admins", ""],
                ["Name", "Phone"],
                ["Eli", "+15714352602"],
            ],
        )
        client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        notification_calls = [
            c for c in mock_sms.call_args_list if "+15714352602" in str(c)
        ]
        assert len(notification_calls) == 0

    @patch.object(app_module.twilio_client.messages, "create")
    @patch.object(app_module, "get_sheet")
    def test_notification_failure_does_not_break_reply(
        self, mock_sheet, mock_sms, client
    ):
        """If notification SMS fails, the reply webhook should still succeed."""
        self._make_sheet_mocks(mock_sheet)
        mock_sms.side_effect = Exception("Twilio error")
        resp = client.post(
            "/sms-reply",
            data={"From": "+15559999999", "Body": "Hello"},
        )
        assert resp.status_code == 200
        assert b"<Response" in resp.data
