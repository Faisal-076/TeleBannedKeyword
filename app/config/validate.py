"""Startup environment validation, scoped per process role.

Each process role (bot, worker, api) only validates the variables it
actually uses. A bot deployment must never be forced to supply
worker-only secrets (TELEGRAM_API_ID/HASH, session material), and vice
versa. Failures raise RuntimeError with a clear message listing exactly
what is missing.
"""

from __future__ import annotations

from app.config import get_settings

ROLE_BOT = "bot"
ROLE_WORKER = "worker"
ROLE_API = "api"

VALID_ROLES = (ROLE_BOT, ROLE_WORKER, ROLE_API)


def validate_role_env(role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role: {role!r}")

    settings = get_settings()
    errors: list[str] = []

    if role == ROLE_BOT:
        if not settings.bot_configured:
            errors.append("BOT_TOKEN is required to run the bot service")
        if not settings.admin_user_ids:
            errors.append(
                "ADMIN_USER_IDS is required to run the bot service "
                "(empty allowlist locks everyone out)"
            )

    elif role == ROLE_WORKER:
        if not settings.mtproto_configured:
            errors.append(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH are required to run the worker"
            )
        # The worker is the single owner of the scanner session. A session
        # can be provisioned after the first deploy (documented bootstrap),
        # so a missing session alone is not fatal -- but if session material
        # exists, MASTER_SECRET must be present to decrypt it.
        if settings.session_configured and not settings.master_secret:
            errors.append(
                "MASTER_SECRET is required to decrypt the session "
                "(SESSION_ENC or SESSION_FILE is set)"
            )

    elif role == ROLE_API:
        if not settings.admin_api_key.get_secret_value():
            errors.append("ADMIN_API_KEY is required to run the API service")

    if errors:
        raise RuntimeError(
            f"invalid configuration for role {role!r}:\n" + "\n".join(f"  - {e}" for e in errors)
        )
