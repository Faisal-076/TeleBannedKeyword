"""Redaction helpers for logs, errors and API responses.

Everything sensitive that must never appear in output goes through here.
"""

from __future__ import annotations

import re
from typing import Any

_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{5,}:[A-Za-z0-9_\-]{30,}\b"), "***BOT_TOKEN***"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "***HEX_SECRET***"),
    (re.compile(r"\b1[A-Za-z0-9_\-]{40,}\b"), "***SESSION***"),
]

_MAX_STRING = 512


def redact_string(value: str) -> str:
    for pattern, placeholder in _SECRET_PATTERNS:
        value = pattern.sub(placeholder, value)
    if len(value) > _MAX_STRING:
        value = value[: _MAX_STRING] + "...(truncated)"
    return value


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v) for v in value]
    return value


def mask_secret(value: str, visible: int = 4) -> str:
    """Return `value` masked to `visible` leading characters."""
    if not value:
        return ""
    if len(value) <= visible + 4:
        return "*" * len(value)
    return value[:visible] + "*" * (len(value) - visible)


def mask_phone(value: str) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7:
        return "*" * len(value)
    return f"+{digits[:3]}***{digits[-2:]}"


def mask_username(value: str | None) -> str:
    if not value:
        return "unknown"
    if value.startswith("@"):
        inner = value[1:]
    else:
        inner = value
    if len(inner) <= 2:
        return "@" + "*" * len(inner)
    return "@" + inner[:1] + "*" * (len(inner) - 1)


def redact_telegram_error(message: str) -> str:
    """Strip possible session/phone fragments from an exception message."""
    message = _PHONE_RE.sub("***PHONE***", message)
    if len(message) > 500:
        message = message[:500] + "...(truncated)"
    return message
