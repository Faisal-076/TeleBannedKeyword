"""Content hashing for deduplication and integrity checks."""

from __future__ import annotations

import hashlib


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
