"""Telegram access-state classification and typed errors.

Every exception carries an `error_code` string so workers and the bot can
degrade gracefully per-chat instead of crashing the pipeline.
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
    code = "flood_wait"
    retryable = True

    def __init__(self, seconds: float):
        super().__init__(f"flood wait {int(seconds)}s", code=self.code, retryable=True)
        self.seconds = seconds


class SessionExpiredError(TelegramAccessError):
    code = "session_expired"
    retryable = False


class NetworkError(TelegramAccessError):
    code = "network_error"
    retryable = True


_KNOWN_MAPPINGS: dict[tuple[str, ...], str] = {
    ("UsernameNotOccupiedError", "UsernameInvalidError"): AccessState.USERNAME_NOT_FOUND.value,
    ("ChannelPrivateError", "ChatIdInvalidError"): AccessState.PRIVATE_NO_ACCESS.value,
    ("ChatAdminRequiredError", "ChatAdminRequiredError"): AccessState.INSUFFICIENT_PERMISSIONS.value,
    ("UserBannedInChannelError"): AccessState.BANNED.value,
    ("ChannelInvalidError", "ChatInvalidError"): AccessState.DELETED.value,
    ("ChatMigrateError"): AccessState.MIGRATED.value,
    ("ChatForbiddenError", "UserNotParticipantError", "ChatParticipantNotFoundError"):
        AccessState.NOT_MEMBER.value,
    ("InviteHashExpiredError", "InviteHashInvalidError"): AccessState.INVALID_INVITE.value,
    ("UserRestrictedError"): AccessState.RESTRICTED.value,
    ("AuthKeyUnregisteredError", "UnauthorizedError"): AccessState.ERROR.value,
}


def classify_access_error(exc: BaseException) -> tuple[str, bool]:
    """Map a caught exception to (access_state_code, retryable)."""
    from telethon.errors import (
        AuthKeyUnregisteredError,
        ChannelPrivateError,
        ChatAdminRequiredError,
        ChatForbiddenError,
        ChatIdInvalidError,
        ChatInvalidError,
        ChatMigrateError,
        FloodWaitError,
        InviteHashExpiredError,
        InviteHashInvalidError,
        UnauthorizedError,
        UserBannedInChannelError,
        UserNotParticipantError,
        UserRestrictedError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
    )

    if isinstance(exc, FloodWaitError):
        return AccessState.ERROR.value, True
    for exc_type, code in (
        (UsernameNotOccupiedError, AccessState.USERNAME_NOT_FOUND.value),
        (UsernameInvalidError, AccessState.USERNAME_NOT_FOUND.value),
        (ChannelPrivateError, AccessState.PRIVATE_NO_ACCESS.value),
        (ChatIdInvalidError, AccessState.PRIVATE_NO_ACCESS.value),
        (ChatAdminRequiredError, AccessState.INSUFFICIENT_PERMISSIONS.value),
        (UserBannedInChannelError, AccessState.BANNED.value),
        (ChatInvalidError, AccessState.DELETED.value),
        (ChatMigrateError, AccessState.MIGRATED.value),
        (ChatForbiddenError, AccessState.NOT_MEMBER.value),
        (UserNotParticipantError, AccessState.NOT_MEMBER.value),
        (InviteHashExpiredError, AccessState.INVALID_INVITE.value),
        (InviteHashInvalidError, AccessState.INVALID_INVITE.value),
        (UserRestrictedError, AccessState.RESTRICTED.value),
        (AuthKeyUnregisteredError, AccessState.ERROR.value),
        (UnauthorizedError, AccessState.ERROR.value),
    ):
        if isinstance(exc, exc_type):
            return code, False
    return AccessState.ERROR.value, False
