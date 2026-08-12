"""MTProto gateway abstraction around Telethon.

Responsibilities:
- persistent connection + reconnect handling
- chat resolution (username / t.me link / invite link / numeric id)
- access-state verification and per-chat graceful degradation
- message search and paginated history iteration
- global + per-chat rate limiting and FloodWait handling

Everything Telegram-specific lives here; the rest of the app depends on this
interface only.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.security.redact import mask_phone, mask_username, redact_telegram_error
from app.telegram.errors import (
    AccessState,
    ChatFloodWaitError,
    NetworkError,
    SessionExpiredError,
    TelegramAccessError,
    classify_access_error,
)
from app.telegram.session_store import SessionStore
from app.utils.timeutil import utcnow

logger = logging.getLogger("app.telegram.gateway")

_TME_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?P<path>joinchat/(?P<hash1>[A-Za-z0-9_\-]+)|"
    r"\+(?P<hash2>[A-Za-z0-9_\-]+)|(?P<username>[A-Za-z0-9_]{3,32})/?)$"
)


@dataclass
class MessageHit:
    message_id: int
    date: datetime | None
    text: str
    topic_id: int | None = None


@dataclass
class ResolvedChat:
    chat_id: int
    title: str
    username: str | None
    chat_type: str
    access_state: AccessState
    error: str | None = None
    entity: Any | None = None


@dataclass
class _ChatGate:
    last_call: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TelegramGateway:
    def __init__(self, session_store: SessionStore):
        self._settings = get_settings()
        self._session_store = session_store
        self._client: Any = None
        self._me: Any = None
        self._last_connected: datetime | None = None
        self._entity_cache: dict[int, Any] = {}
        self._sem = asyncio.Semaphore(self._settings.mt_proto_max_concurrency)
        self._chat_gates: dict[int, _ChatGate] = {}
        self._gate_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected())

    @property
    def last_connected(self) -> datetime | None:
        return self._last_connected

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.connected:
                return True
            if await self._session_store.is_revoked():
                logger.warning("telegram: session revoked, refusing to connect")
                return False
            session_string = await self._session_store.load()
            if not session_string:
                logger.warning("telegram: no session available")
                return False
            if self._client is None:
                from telethon import TelegramClient
                from telethon.sessions import StringSession

                self._client = TelegramClient(
                    StringSession(session_string),
                    self._settings.telegram_api_id,
                    self._settings.telegram_api_hash.get_secret_value(),
                    flood_sleep_threshold=self._settings.mt_proto_flood_sleep_threshold,
                    connection_retries=6,
                    retry_delay=2,
                    request_retries=4,
                )
            try:
                await self._client.connect()
                me = await self._client.get_me()
                if me is None:
                    logger.error("telegram: get_me returned None (session invalid)")
                    self._client = None
                    return False
                self._me = me
                self._last_connected = utcnow()
                logger.info(
                    "telegram: mtproto connected",
                    extra={"extra": {"account": mask_username(me.username)}},
                )
                return True
            except Exception as exc:  # noqa: BLE001
                code, retryable = classify_access_error(exc)
                if code == AccessState.ERROR.value and not retryable:
                    raise SessionExpiredError(redact_telegram_error(str(exc))) from exc
                logger.error(
                    "telegram: connect failed: %s",
                    redact_telegram_error(str(exc)),
                    extra={"extra": {"code": code}},
                )
                return False

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def ensure_connected(self) -> None:
        if not await self.connect():
            raise TelegramAccessError("mtproto not connected", code=AccessState.ERROR.value)

    async def get_me_info(self) -> dict:
        await self.ensure_connected()
        if not self._me:
            return {"connected": False}
        phone = getattr(self._me, "phone", None)
        return {
            "connected": True,
            "username": mask_username(getattr(self._me, "username", None)),
            "first_name_masked": bool(getattr(self._me, "first_name", None)),
            "phone_masked": mask_phone(phone) if phone else None,
            "dc_id": getattr(self._me, "photo", None) and None,
            "last_connected": self._last_connected.isoformat() if self._last_connected else None,
        }

    # ------------------------------------------------------------------ rate limiting

    async def _space_chat_calls(self, chat_id: int) -> None:
        async with self._gate_lock:
            gate = self._chat_gates.setdefault(chat_id, _ChatGate())
        async with gate.lock:
            elapsed = time.monotonic() - gate.last_call
            interval = self._settings.mt_proto_chat_min_interval
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            gate.last_call = time.monotonic()

    async def _call(self, chat_id: int | None, factory):
        """Run a Telethon coroutine with rate limiting + flood/backoff retry."""
        await self.ensure_connected()
        assert self._client is not None
        attempts = 0
        while True:
            attempts += 1
            try:
                async with self._sem:
                    if chat_id is not None:
                        await self._space_chat_calls(chat_id)
                    return await factory(self._client)
            except Exception as exc:  # noqa: BLE001
                code, retryable = classify_access_error(exc)
                if retryable and attempts < self._settings.mt_proto_retry_limit:
                    delay = min(2.0**attempts + random.uniform(0, 1), 30.0)
                    logger.warning(
                        "telegram: transient failure, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempts,
                        self._settings.mt_proto_retry_limit,
                        extra={"extra": {"code": code}},
                    )
                    await asyncio.sleep(delay)
                    continue
                message = redact_telegram_error(str(exc))
                if code == "flood_wait":
                    seconds = float(getattr(exc, "seconds", 60) or 60)
                    raise ChatFloodWaitError(seconds) from exc
                if code == AccessState.ERROR.value:
                    raise SessionExpiredError(message) if not retryable else NetworkError(message)
                raise TelegramAccessError(message, code=code, retryable=retryable) from exc

    # ------------------------------------------------------------------ entity / chat

    async def _entity_for(self, chat_id: int) -> Any:
        if chat_id in self._entity_cache:
            return self._entity_cache[chat_id]
        peer = _peer_for_id(chat_id)
        entity = await self._call(chat_id, lambda client: client.get_entity(peer))
        self._entity_cache[chat_id] = entity
        return entity

    async def get_chat_info(self, chat_id: int) -> ResolvedChat:
        try:
            entity = await self._entity_for(chat_id)
            return _resolved_from_entity(entity, AccessState.ACCESSIBLE)
        except TelegramAccessError as exc:
            return ResolvedChat(
                chat_id=chat_id,
                title="",
                username=None,
                chat_type="unknown",
                access_state=AccessState(exc.code) if exc.code in AccessState._value2member_map_ else AccessState.ERROR,
                error=exc.code,
            )

    async def resolve_chat(self, raw: str | int) -> ResolvedChat:
        """Resolve a user-provided reference to a chat with access verification.

        Accepted forms:
          @username, username, https://t.me/username
          https://t.me/+invite_hash, https://t.me/joinchat/hash
          numeric chat id
        """
        await self.ensure_connected()
        assert self._client is not None
        try:
            if isinstance(raw, int):
                entity = await self._entity_for(raw)
                return _resolved_from_entity(entity, AccessState.ACCESSIBLE)

            text = str(raw).strip()
            link = _TME_LINK_RE.match(text)
            if link:
                invite_hash = link.group("hash1") or link.group("hash2")
                if invite_hash:
                    return await self._resolve_invite(invite_hash)
                username = link.group("username")
                entity = await self._call(None, lambda client: client.get_entity(username))
                return _resolved_from_entity(entity, AccessState.ACCESSIBLE)

            if text.startswith("@") or (text and text[0].isalnum() and " " not in text):
                candidate = text if text.startswith("@") else text
                entity = await self._call(None, lambda client: client.get_entity(candidate))
                return _resolved_from_entity(entity, AccessState.ACCESSIBLE)

            return ResolvedChat(
                chat_id=0,
                title="",
                username=None,
                chat_type="unknown",
                access_state=AccessState.ERROR,
                error="unsupported_reference",
            )
        except TelegramAccessError as exc:
            return ResolvedChat(
                chat_id=0,
                title="",
                username=None,
                chat_type="unknown",
                access_state=AccessState(exc.code) if exc.code in AccessState._value2member_map_ else AccessState.ERROR,
                error=exc.code,
            )

    async def _resolve_invite(self, invite_hash: str) -> ResolvedChat:
        assert self._client is not None
        from telethon import functions

        try:
            result = await self._call(
                None,
                lambda client: client(functions.messages.CheckChatInviteRequest(hash=invite_hash)),
            )
            entity = getattr(result, "chat", None)
            if entity is None:
                # ChatInvite (not joined yet) → join now
                invite = result
                entity = getattr(invite, "chat", None)
                if entity is None:
                    return ResolvedChat(
                        chat_id=0, title="", username=None, chat_type="unknown",
                        access_state=AccessState.INVALID_INVITE, error="invalid_invite",
                    )
                await self._call(
                    None,
                    lambda client: client(functions.messages.JoinChannelRequest(channel=entity)),
                )
            return _resolved_from_entity(entity, AccessState.ACCESSIBLE)
        except TelegramAccessError as exc:
            return ResolvedChat(
                chat_id=0, title="", username=None, chat_type="unknown",
                access_state=AccessState(exc.code) if exc.code in AccessState._value2member_map_ else AccessState.ERROR,
                error=exc.code,
            )

    # ------------------------------------------------------------------ messages

    async def search_messages(self, chat_id: int, query: str, limit: int = 50) -> list[MessageHit]:
        """Targeted Telegram search (messages.search via Telethon)."""
        query = query.strip()
        if not query:
            return []
        entity = await self._entity_for(chat_id)
        hits: list[MessageHit] = []

        async def _search(client) -> list[MessageHit]:
            collected = []
            async for msg in client.iter_messages(entity, search=query, limit=limit):
                text = msg.message or ""
                if text.strip():
                    collected.append(
                        MessageHit(
                            message_id=msg.id,
                            date=msg.date,
                            text=text,
                            topic_id=_topic_of(msg),
                        )
                    )
            return collected

        try:
            hits = await self._call(chat_id, _search)
        except TelegramAccessError as exc:
            if exc.code in ("insufficient_permissions", "private_no_access"):
                logger.info(
                    "telegram: search unavailable for chat %s (%s); falling back to local index",
                    chat_id,
                    exc.code,
                )
                return []
            raise
        return hits[:limit]

    async def iter_messages(
        self,
        chat_id: int,
        *,
        min_id: int | None = None,
        limit: int = 500,
        topic_id: int | None = None,
    ) -> list[MessageHit]:
        """Paginated history iteration (ascending from min_id for incremental sync)."""
        entity = await self._entity_for(chat_id)

        async def _iterate(client) -> list[MessageHit]:
            collected = []
            kwargs: dict[str, Any] = {"min_id": min_id or 0, "limit": limit, "reverse": True}
            if topic_id is not None:
                try:
                    kwargs["reply_to"] = topic_id
                except TypeError:  # pragma: no cover - older telethon
                    pass
            try:
                iterator = client.iter_messages(entity, **kwargs)
                async for msg in iterator:
                    text = msg.message or ""
                    if not text.strip():
                        continue
                    if topic_id is not None and _topic_of(msg) not in (None, topic_id):
                        continue
                    collected.append(
                        MessageHit(
                            message_id=msg.id,
                            date=msg.date,
                            text=text,
                            topic_id=_topic_of(msg),
                        )
                    )
                    if len(collected) >= limit:
                        break
            except TypeError:
                # `reply_to` not supported by this telethon version → client-side filter
                iterator = client.iter_messages(entity, min_id=min_id or 0, limit=limit, reverse=True)
                async for msg in iterator:
                    text = msg.message or ""
                    if not text.strip():
                        continue
                    if topic_id is not None and _topic_of(msg) != topic_id:
                        continue
                    collected.append(
                        MessageHit(
                            message_id=msg.id,
                            date=msg.date,
                            text=text,
                            topic_id=_topic_of(msg),
                        )
                    )
                    if len(collected) >= limit:
                        break
            return collected

        try:
            return await self._call(chat_id, _iterate)
        except TelegramAccessError as exc:
            if exc.code == "flood_wait":
                raise
            raise TelegramAccessError(
                f"history unavailable for chat {chat_id}: {exc.code}",
                code=exc.code,
                retryable=exc.retryable,
            ) from exc

    async def estimate_total(self, chat_id: int) -> int | None:
        """Total message count estimate (from GetHistory total)."""
        entity = await self._entity_for(chat_id)

        async def _estimate(client) -> int | None:
            messages = await client.get_messages(entity, limit=1)
            total = getattr(messages, "total", None)
            return int(total) if total else None

        try:
            return await self._call(chat_id, _estimate)
        except TelegramAccessError as exc:
            logger.info("telegram: cannot estimate history size chat=%s code=%s", chat_id, exc.code)
            return None

    async def get_message(self, chat_id: int, message_id: int) -> MessageHit | None:
        entity = await self._entity_for(chat_id)

        async def _get(client) -> MessageHit | None:
            msg = await client.get_messages(entity, ids=message_id)
            if msg is None:
                return None
            return MessageHit(
                message_id=msg.id,
                date=msg.date,
                text=msg.message or "",
                topic_id=_topic_of(msg),
            )

        try:
            return await self._call(chat_id, _get)
        except TelegramAccessError as exc:
            logger.info("telegram: get_message failed chat=%s msg=%s code=%s", chat_id, message_id, exc.code)
            return None


# ------------------------------------------------------------------ helpers


def _peer_for_id(chat_id: int):
    from telethon.tl.types import PeerChannel, PeerChat, PeerUser

    if chat_id < -1_000_000_000_000:
        return PeerChannel(-(chat_id + 1_000_000_000_000))
    if chat_id < 0:
        return PeerChat(-chat_id)
    return PeerUser(chat_id)


def _resolved_from_entity(entity: Any, access_state: AccessState) -> ResolvedChat:
    chat_id = getattr(entity, "id", 0)
    if getattr(entity, "is_channel", False):
        chat_id = -1_000_000_000_000 - int(chat_id)
    elif getattr(entity, "is_group", False):
        chat_id = -int(chat_id)
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or ""
    username = getattr(entity, "username", None)
    if hasattr(entity, "megagroup") and entity.megagroup:
        chat_type = "forum" if getattr(entity, "forum", False) else "supergroup"
    elif getattr(entity, "is_channel", False):
        chat_type = "channel"
    elif getattr(entity, "is_group", False):
        chat_type = "group"
    elif getattr(entity, "is_user", False):
        chat_type = "user"
    else:
        chat_type = "unknown"
    return ResolvedChat(
        chat_id=chat_id,
        title=title or "",
        username=username,
        chat_type=chat_type,
        access_state=access_state,
        entity=entity,
    )


def _topic_of(msg: Any) -> int | None:
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None:
        value = getattr(reply_to, "reply_to_msg_id", None)
        if value is not None and int(value) > 1:
            return int(value)
    return None


def create_gateway() -> TelegramGateway:
    return TelegramGateway(SessionStore())
