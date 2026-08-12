"""Shared fixtures: isolated SQLite database, fake Telegram gateway, env."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "LOG_LEVEL": "ERROR",
    "LOG_PRIVACY_LEVEL": "minimal",
    "BOT_TOKEN": "123456:TESTTOKENabcdefghijklmnopqrstuvwxyz0123456789",
    "ADMIN_USER_IDS": "111,222",
    "ADMIN_API_KEY": "test-admin-key-secret",
    "TELEGRAM_API_ID": "12345",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "MASTER_SECRET": "test-master-secret-not-for-production",
    "SESSION_ENC": "v1:dGVzdA",  # invalid blob; session tests set their own
    "FUZZY_THRESHOLD": "0.88",
    "LLM_PROVIDER": "disabled",
}


@pytest.fixture(scope="session", autouse=True)
def _env(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    _TEST_ENV["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    _TEST_ENV["REDIS_URL"] = "redis://127.0.0.1:1/15"  # refuses connections instantly
    for key, value in _TEST_ENV.items():
        os.environ[key] = value

    from app.config import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="session")
async def db(_env):
    from app.database.engine import dispose_engine, get_engine
    from app.database.models import Base

    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
async def _clean_tables(db):
    """Truncate all tables between tests."""
    from sqlalchemy import delete

    from app.database.engine import get_session_factory
    from app.database.models import (
        AnalysisRequest,
        AnalysisResult,
        AuditEvent,
        IndexedMessage,
        MessageTerm,
        PhraseOccurrence,
        Rule,
        TargetChat,
        UserSetting,
    )

    factory = get_session_factory()
    async with factory() as session:
        for model in (
            AnalysisResult,
            AnalysisRequest,
            MessageTerm,
            PhraseOccurrence,
            IndexedMessage,
            Rule,
            TargetChat,
            UserSetting,
            AuditEvent,
        ):
            await session.execute(delete(model))
        await session.commit()
    yield


@dataclass
class FakeMessage:
    message_id: int
    date: datetime
    text: str
    topic_id: int | None = None


@dataclass
class FakeGateway:
    """In-memory Telegram stand-in: searchable, paginated message store."""

    messages: dict[int, list[FakeMessage]] = field(default_factory=dict)
    chat_meta: dict[int, dict] = field(default_factory=dict)
    raise_on_search: Exception | None = None
    raise_on_iter: Exception | None = None
    connected: bool = True

    def add_messages(self, chat_id: int, messages: list[FakeMessage]) -> None:
        self.messages.setdefault(chat_id, []).extend(messages)

    async def search_messages(self, chat_id: int, query: str, limit: int = 50):
        if self.raise_on_search:
            raise self.raise_on_search
        query = query.casefold()
        hits = [
            m
            for m in self.messages.get(chat_id, [])
            if query in m.text.casefold()
        ]
        return hits[:limit]

    async def iter_messages(self, chat_id: int, *, min_id: int | None = None, limit: int = 500, topic_id: int | None = None):
        if self.raise_on_iter:
            raise self.raise_on_iter
        items = sorted(
            [m for m in self.messages.get(chat_id, []) if m.message_id > (min_id or 0)],
            key=lambda m: m.message_id,
        )
        if topic_id is not None:
            items = [m for m in items if m.topic_id == topic_id]
        return items[:limit]

    async def estimate_total(self, chat_id: int):
        return len(self.messages.get(chat_id, []))

    async def get_chat_info(self, chat_id: int):
        from app.telegram.errors import AccessState

        meta = self.chat_meta.get(chat_id, {})
        return _Resolved(chat_id, meta.get("title", "fake"), meta.get("username"),
                         meta.get("chat_type", "group"), AccessState.ACCESSIBLE, None)

    async def resolve_chat(self, raw):
        from app.telegram.errors import AccessState

        if isinstance(raw, int):
            meta = self.chat_meta.get(raw, {})
        else:
            ref = str(raw).strip().lstrip("@").lower()
            if "/+" in ref or ref.startswith("+") or "joinchat" in ref:
                return _Resolved(
                    0, "", None, "unknown",
                    AccessState.PRIVATE_NO_ACCESS, None, "private_no_access",
                )
            meta = next(
                (m for m in self.chat_meta.values() if (m.get("username") or "").lower() == ref),
                {},
            )
        if not meta:
            return _Resolved(0, "", None, "unknown", AccessState.ERROR, None)
        return _Resolved(meta["id"], meta["title"], meta.get("username"),
                         meta.get("chat_type", "group"), AccessState.ACCESSIBLE, None)

    async def get_me_info(self):
        return {"connected": self.connected, "username": "@fake", "last_connected": "2026-01-01T00:00:00+00:00"}

    @property
    def last_connected(self):
        return None

    async def connect(self):
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False


@dataclass
class _Resolved:
    chat_id: int
    title: str
    username: str | None
    chat_type: str
    access_state: object
    entity: object = None
    error: str | None = None


def make_message(chat_id: int, message_id: int, text: str, *, days_ago: int = 0, topic_id: int | None = None) -> FakeMessage:
    return FakeMessage(
        message_id=message_id,
        date=datetime.now(UTC) - timedelta(days=days_ago),
        text=text,
        topic_id=topic_id,
    )


@pytest.fixture
def fake_gateway():
    return FakeGateway()
