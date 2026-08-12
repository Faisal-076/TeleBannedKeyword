"""Bot-visible session state, database only.

The bot process never touches scanner session material (no SESSION_ENC /
SESSION_FILE / MASTER_SECRET, no session volume). It only reads and writes
the revocation flag in Postgres (`app_state.telegram_session`); the worker —
the single owner of the scanner session — checks the same flag before
connecting and is the only process that decrypts or wipes the session file.

The key is intentionally the same string used by the worker's SessionStore;
`tests/unit/test_deployment.py::test_bot_session_state_db_only` pins that
the two views agree.
"""

from __future__ import annotations

from app.database.engine import session_scope
from app.database.models import AppState

SESSION_STATE_KEY = "telegram_session"


async def session_revoked() -> bool:
    """Return True when the worker must refuse to connect the scanner."""
    async with session_scope() as session:
        state = await session.get(AppState, SESSION_STATE_KEY)
        return bool(state and state.value.get("revoked"))


async def mark_session_revoked() -> None:
    """Set the revocation flag in the database (bot-side fallback).

    Used only when the worker cannot be notified via Redis (queue down).
    The worker refuses to reconnect while the flag is set; provisioning via
    `tbk-auth` clears it.
    """
    async with session_scope() as session:
        state = await session.get(AppState, SESSION_STATE_KEY)
        if state is None:
            state = AppState(key=SESSION_STATE_KEY, value={})
            session.add(state)
        state.value = {**state.value, "revoked": True}