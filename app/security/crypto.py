"""Authenticated encryption for the Telethon session blob.

Scheme: AES-256-GCM (RFC 8452 style usage via `cryptography`), key derived
from the operator-supplied master secret with HKDF-SHA256 plus a fixed
context/salt. Output format: `v1:<base64(nonce || ciphertext || tag)>`.

The master secret lives only in environment/secret management (MASTER_SECRET)
and is never stored in the database or in source code.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "v1:"
_SALT_CONTEXT = b"telegram-message-analyzer/session/v1"
_NONCE_LEN = 12


class CryptoError(Exception):
    """Raised for any encryption/decryption failure."""


def derive_key(master_secret: str, purpose: bytes = _SALT_CONTEXT) -> bytes:
    """Derive a 32-byte key from the master secret via HKDF-SHA256."""
    if not master_secret:
        raise CryptoError("master secret is empty")
    salt = hashlib.sha256(purpose).digest()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"tbk-key-v1",
    )
    return hkdf.derive(master_secret.encode("utf-8"))


def encrypt_blob(plaintext: bytes, key: bytes) -> str:
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_blob(blob: str, key: bytes) -> bytes:
    if not blob.startswith(PREFIX):
        raise CryptoError("unsupported ciphertext format")
    try:
        raw = base64.urlsafe_b64decode(blob[len(PREFIX):].encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("malformed ciphertext") from exc
    if len(raw) < _NONCE_LEN + 16:
        raise CryptoError("ciphertext too short")
    nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise CryptoError("authentication failed (wrong key or tampered data)") from exc


def encrypt_string(plaintext: str, master_secret: str) -> str:
    return encrypt_blob(plaintext.encode("utf-8"), derive_key(master_secret))


def decrypt_string(blob: str, master_secret: str) -> str:
    return decrypt_blob(blob, derive_key(master_secret)).decode("utf-8")
