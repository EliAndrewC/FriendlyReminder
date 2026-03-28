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
        app_module, "get_phone_numbers", return_value=["+15559990001", "+15559990002"]
    )
    @patch.object(app_module.twilio_client.messages, "create")
    def test_send_test_mode(self, mock_sms, mock_numbers, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        mock_numbers.assert_called_once_with("Test")
        assert mock_sms.call_count == 2
        # Verify STOP suffix appended
        sent_body = mock_sms.call_args_list[0][1]["body"]
        assert sent_body == "Hello\nReply STOP to unsubscribe"

    @patch.object(app_module, "get_phone_numbers", return_value=["+15559990001"])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_send_real_mode(self, mock_sms, mock_numbers, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "real"})
        assert resp.status_code == 200
        mock_numbers.assert_called_once_with("Recipients")
        assert mock_sms.call_count == 1

    @patch.object(
        app_module,
        "get_phone_numbers",
        return_value=["+15559990001", "+15559990002", "+15559990003"],
    )
    @patch.object(app_module.twilio_client.messages, "create")
    def test_sent_page_shows_count(self, mock_sms, mock_numbers, client):
        login(client)
        resp = client.post("/send", data={"message": "Hi", "mode": "test"})
        assert b"3" in resp.data  # sent_count and total
        assert b"Test messages sent" in resp.data

    @patch.object(app_module, "get_phone_numbers", return_value=["+15559990001"])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_sent_page_real_mode_heading(self, mock_sms, mock_numbers, client):
        login(client)
        resp = client.post("/send", data={"message": "Hi", "mode": "real"})
        assert b"Messages sent" in resp.data

    @patch.object(
        app_module, "get_phone_numbers", return_value=["+15559990001", "+15559990002"]
    )
    @patch.object(
        app_module.twilio_client.messages,
        "create",
        side_effect=[None, Exception("Twilio error")],
    )
    def test_send_with_partial_failure(self, mock_sms, mock_numbers, client):
        login(client)
        resp = client.post("/send", data={"message": "Hello", "mode": "test"})
        assert resp.status_code == 200
        assert b"1" in resp.data  # 1 of 2 sent
        assert b"Twilio error" in resp.data

    @patch.object(app_module, "get_phone_numbers", return_value=[])
    @patch.object(app_module.twilio_client.messages, "create")
    def test_send_to_empty_list(self, mock_sms, mock_numbers, client):
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
            app_module, "get_phone_numbers", return_value=["+15559990001"]
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
