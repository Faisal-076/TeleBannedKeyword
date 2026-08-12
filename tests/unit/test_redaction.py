from app.logging_conf import RedactingJsonFormatter, hash_user_id
from app.security.redact import mask_phone, mask_secret, mask_username, redact_string
import json
import logging


def test_mask_secret():
    assert mask_secret("1234567890") == "1234******"
    assert mask_secret("ab") == "**"


def test_mask_phone():
    assert mask_phone("+12345678901") == "+123***01"


def test_mask_username():
    assert mask_username("myusername") == "@m*********"
    assert mask_username(None) == "unknown"


def test_redact_string_bot_token():
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrst"
    assert token not in redact_string(token)


def test_redact_string_hex_secret():
    secret = "f" * 40
    assert secret not in redact_string(secret)


def test_hash_user_id_deterministic():
    assert hash_user_id(42) == hash_user_id("42")
    assert hash_user_id(42) != hash_user_id(43)


def test_json_formatter_redacts_extras():
    formatter = RedactingJsonFormatter()
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1,
        "operation done", None, None,
    )
    record.extra = {"user_id": 42, "token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrst"}
    output = formatter.format(record)
    data = json.loads(output)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in output
    assert data["extra"]["user_id"] == hash_user_id(42)


def test_json_formatter_full_privacy_shows_raw_user_id():
    formatter = RedactingJsonFormatter()
    formatter.set_privacy_level("full")
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "x", None, None)
    record.extra = {"user_id": 42}
    data = json.loads(formatter.format(record))
    assert data["extra"]["user_id"] == 42
