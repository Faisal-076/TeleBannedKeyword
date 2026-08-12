"""Telegram access-state classification and typed errors.

Every exception carries an `error_code` string so workers and the bot can
degrade gracefully per-chat instead of crashing the pipeline.

Classification contract:
- flood_wait      → ChatFloodWaitError (bounded; operator alert)
- session_expired → SessionExpiredError (auth key revoked/unregistered)
- network_error   → NetworkError (transient, retryable)
- anything else   → TelegramAccessError with the mapped access state
"""

from __future__ import annotations

from enum import Enum


class AccessState(str, Enum):
    ACCESSIBLE = "accessible"
    NOT_MEMBER = "not_member"
    BANNED = "banned"
    RESTRICTED = "restricted"
    PRIVATE_NO_ACCESS = "private_no_access"
    DELETED = "deleted"
    MIGRATED = "migrated"
    USERNAME_NOT_FOUND = "username_not_found"
    INSUFFICIENT_PERMISSIONS = "insufficient_permissions"
    INVALID_INVITE = "invalid_invite"
    NOT_VERIFIED = "not_verified"
    ERROR = "error"


class TelegramAccessError(Exception):
    """A typed failure while talking to Telegram."""

    code: str = AccessState.ERROR.value
    retryable: bool = False

    def __init__(self, message: str = "", *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class ChatFloodWaitError(TelegramAccessError):
    """Telegram asked us to wait; the wait is capped and reported."""

    code = "flood_wait"
    retryable = False  # retrying sooner than `seconds` is pointless

    def __init__(self, seconds: float):
        super().__init__(f"flood wait {int(seconds)}s", code=self.code, retryable=False)
        self.seconds = seconds


class SessionExpiredError(TelegramAccessError):
    """The scanner session is invalid/revoked; provisioning is required."""

    code = "session_expired"
    retryable = False


class NetworkError(TelegramAccessError):
    """Transient transport/unknown failure; safe to retry with backoff."""

    code = "network_error"
    retryable = True


def classify_access_error(exc: BaseException) -> tuple[str, bool]:
    """Map a caught exception to (error_code, retryable)."""
    from telethon import errors

    if isinstance(exc, errors.FloodWaitError):
        return "flood_wait", False

    access_mappings = (
        (errors.UsernameNotOccupiedError, AccessState.USERNAME_NOT_FOUND.value),
        (errors.UsernameInvalidError, AccessState.USERNAME_NOT_FOUND.value),
        (errors.ChannelPrivateError, AccessState.PRIVATE_NO_ACCESS.value),
        (errors.ChatIdInvalidError, AccessState.PRIVATE_NO_ACCESS.value),
        (errors.ChatAdminRequiredError, AccessState.INSUFFICIENT_PERMISSIONS.value),
        (errors.UserBannedInChannelError, AccessState.BANNED.value),
        (errors.ChatInvalidError, AccessState.DELETED.value),
        (errors.ChatForbiddenError, AccessState.NOT_MEMBER.value),
        (errors.UserNotParticipantError, AccessState.NOT_MEMBER.value),
        (errors.InviteHashExpiredError, AccessState.INVALID_INVITE.value),
        (errors.InviteHashInvalidError, AccessState.INVALID_INVITE.value),
        (errors.UserRestrictedError, AccessState.RESTRICTED.value),
        (errors.AuthKeyUnregisteredError, "session_expired"),
        (errors.UnauthorizedError, "session_expired"),
        (errors.AccessTokenInvalidError, "session_expired"),
    )
    for exc_type, code in access_mappings:
        if isinstance(exc, exc_type):
            return code, False

    # Any other RPC/transport failure is treated as transient (retryable);
    # a persistent problem surfaces after MT_PROTO_RETRY_LIMIT attempts as
    # a NetworkError instead of a false "session expired" alarm.
    return "network_error", True
