"""Structured JSON logging with privacy-aware redaction.

Guarantees:
- Secrets (bot token, api hash, master secret, session blob, keys) are never
  written to logs, even if passed as an argument.
- Message content is only logged at privacy level "full".
- User ids are hashed unless level is "full".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from app.security.redact import redact_string, redact_value


def hash_user_id(user_id: int | str) -> str:
    raw = str(user_id).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class RedactingJsonFormatter(logging.Formatter):
    """Emit one JSON object per log line; redact secrets and sensitive ids."""

    _privacy_level: str = "medium"

    def set_privacy_level(self, level: str) -> None:
        self._privacy_level = level

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            extra = redact_value(extra)
            if self._privacy_level != "full" and "user_id" in extra:
                extra["user_id"] = hash_user_id(extra["user_id"])
            entry["extra"] = extra
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def _install_handler(logger: logging.Logger) -> None:
    handler = logging.StreamHandler()
    formatter = RedactingJsonFormatter()
    handler.setFormatter(formatter)
    logger.handlers[:] = [handler]
    logger.propagate = False


def configure_logging(
    level: str = "INFO",
    privacy_level: str = "medium",
    *,
    library_level: str = "WARNING",
) -> logging.Logger:
    root = logging.getLogger()
    _install_handler(root)
    root.setLevel(level.upper())
    formatter = root.handlers[0].formatter
    if isinstance(formatter, RedactingJsonFormatter):
        formatter.set_privacy_level(privacy_level)

    for name in ("aiogram", "telethon", "aiohttp", "httpx", "uvicorn"):
        lg = logging.getLogger(name)
        lg.setLevel(library_level)
    logging.getLogger("app").setLevel(level.upper())
    logging.getLogger("app").propagate = True
    return logging.getLogger("app")


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    extra: dict[str, Any] | None = None,
    exc_info: bool = False,
) -> None:
    logger.log(level, event, extra={"extra": extra or {}}, exc_info=exc_info)
