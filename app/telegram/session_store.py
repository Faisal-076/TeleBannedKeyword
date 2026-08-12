"""Session provisioning and secure in-memory session loading.

The Telethon session string is never stored in plaintext:
- `SESSION_ENC` env var: AES-256-GCM encrypted blob produced by `tbk-auth`.
- `SESSION_FILE` path: encrypted blob on a persistent volume (Railway volume).

Only the decrypted string exists in memory for the lifetime of the process.
`/logout` marks the session revoked in the database and wipes the file.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from app.config import get_settings
from app.database.models import AppState
from app.security.crypto import CryptoError, decrypt_string

logger = logging.getLogger("app.telegram.session")

_SESSION_STATE_KEY = "telegram_session"


@dataclass
class SessionStore:
    _plaintext: str | None = field(default=None, init=False)
    _loaded: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def load(self) -> str | None:
        """Return the decrypted session string (memory-only), or None.

        A `None` result is never cached: after `/logout` wipes the file, a
        freshly provisioned session (new `SESSION_FILE` / `SESSION_ENC` +
        cleared revoked flag) is picked up on the next call without needing a
        worker restart (revoke -> re-provision transition).
        """
        async with self._lock:
            if self._loaded and self._plaintext is not None:
                return self._plaintext
            self._plaintext = await asyncio.to_thread(self._read_from_secrets)
            self._loaded = True
            logger.info("session: loaded", extra={"extra": {"present": bool(self._plaintext)}})
            return self._plaintext

    def _read_from_secrets(self) -> str | None:
        settings = get_settings()
        master = settings.master_secret.get_secret_value() if settings.master_secret else ""
        blob: str | None = settings.session_enc
        path: str | None = settings.session_file
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    blob = fh.read().strip()
            except OSError:
                logger.error("session: cannot read session file", extra={"extra": {"path": path}})
                return None
        if not blob:
            return None
        if not master:
            logger.error("session: MASTER_SECRET is required to decrypt the session")
            return None
        try:
            plain = decrypt_string(blob, master)
        except CryptoError as exc:
            logger.error("session: decryption failed: %s", exc)
            return None
        if not plain:
            return None
        return plain

    async def save_new_session(self, session_string: str) -> None:
        """Persist an encrypted session (used by the provisioning CLI locally)."""
        settings = get_settings()
        master = settings.master_secret.get_secret_value() if settings.master_secret else ""
        if not master:
            raise RuntimeError("MASTER_SECRET is required to encrypt a session")
        from app.security.crypto import encrypt_string

        blob = encrypt_string(session_string, master)
        if settings.session_file:
            path = settings.session_file
            directory = os.path.dirname(path) or "."
            os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(blob)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            logger.info("session: saved encrypted to file")
        else:
            print("SESSION_ENC=" + blob)
            logger.info("session: printed encrypted blob (stdout)")

    async def is_revoked(self) -> bool:
        from app.database.engine import session_scope

        async with session_scope() as session:
            state = await session.get(AppState, _SESSION_STATE_KEY)
            return bool(state and state.value.get("revoked"))

    async def revoke(self) -> None:
        from app.database.engine import session_scope

        async with session_scope() as session:
            state = await session.get(AppState, _SESSION_STATE_KEY)
            if state is None:
                state = AppState(key=_SESSION_STATE_KEY, value={})
                session.add(state)
            state.value = {**state.value, "revoked": True}
        if get_settings().session_file:
            try:
                os.remove(get_settings().session_file)  # type: ignore[arg-type]
            except OSError:
                pass
        self._plaintext = None
        self._loaded = True
        logger.warning("session: revoked locally; rotation of MASTER_SECRET recommended")

    async def unrevoke(self) -> None:
        from app.database.engine import session_scope

        async with session_scope() as session:
            state = await session.get(AppState, _SESSION_STATE_KEY)
            if state is None:
                state = AppState(key=_SESSION_STATE_KEY, value={})
                session.add(state)
            state.value = {**state.value, "revoked": False}
        self._loaded = False
        logger.info("session: unrevoked")
