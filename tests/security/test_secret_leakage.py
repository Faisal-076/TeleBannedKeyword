"""Security tests: authorization, secret leakage prevention, validation."""

from app.bot.middleware import user_authorized
from app.config import get_settings


def test_admin_allowlist_authorized():
    assert user_authorized(111) is True
    assert user_authorized(222) is True


def test_unknown_user_denied():
    assert user_authorized(999) is False
    assert user_authorized(None) is False


def test_settings_never_expose_secrets():
    settings = get_settings()
    dumped = str(settings)
    assert "TESTTOKENabcdefghijklmnopqrstuvwxyz" not in dumped
    assert settings.bot_token.get_secret_value() not in dumped
    assert settings.admin_api_key.get_secret_value() not in dumped
    assert settings.master_secret.get_secret_value() not in dumped


def test_describe_sensitive_has_no_secrets():
    settings = get_settings()
    description = str(settings.describe_sensitive())
    assert "TESTTOKEN" not in description
    assert settings.bot_token.get_secret_value() not in description


def test_session_never_returns_credentials_in_status():
    settings = get_settings()
    assert settings.session_configured is True  # SESSION_ENC set
    status = settings.describe_sensitive()
    assert status["session_configured"] is True
    assert "v1:" not in str(status)


def test_untrusted_input_size_limit():
    settings = get_settings()
    assert settings.max_message_chars <= 4096
