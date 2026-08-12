"""Architecture guarantees: single MTProto owner (the worker).

The bot and API processes must never:
- open an MTProto session / connect a gateway
- join chats via invite links
- run MTProto-backed analysis inline

Plus: FloodWait bounds, session-expiry typing, degraded submit, queue
recovery, and worker chat jobs.
"""

from __future__ import annotations

import pytest

from app.analysis.models import EvidenceType, Finding
from app.config import get_settings
from app.database.models import AnalysisRequest, AppState
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.telegram.errors import (
    AccessState,
    ChatFloodWaitError,
    NetworkError,
    SessionExpiredError,
    TelegramAccessError,
    classify_access_error,
)
from app.telegram.gateway import TelegramGateway
from app.telegram.session_store import SessionStore


async def _noop_notify(*args, **kwargs):
    return None


# ---------------------------------------------------------------- bot isolation


@pytest.fixture(scope="session")
def bot_dispatcher():
    """One dispatcher for the whole session: aiogram routers are module
    singletons and cannot be attached to a second Dispatcher."""
    from app.bot.bot_factory import build_dispatcher

    dispatcher, bot = build_dispatcher(AnalysisService(), ChatService())
    assert bot is None or bot.token  # just must construct
    assert "gateway" not in dispatcher.workflow_data
    assert "session_store" not in dispatcher.workflow_data
    assert "analysis" in dispatcher.workflow_data
    assert "chats" in dispatcher.workflow_data
    return dispatcher


async def test_bot_dispatcher_has_no_gateway(bot_dispatcher):
    assert "gateway" not in bot_dispatcher.workflow_data
    assert "session_store" not in bot_dispatcher.workflow_data
    assert "analysis" in bot_dispatcher.workflow_data
    assert "chats" in bot_dispatcher.workflow_data


async def test_bot_dispatcher_authorizes_message_and_callback_events(bot_dispatcher):
    """Auth middleware must receive events with a direct ``from_user``."""
    from app.bot.middleware import AuthorizationMiddleware, OperationLogMiddleware

    for observer in (bot_dispatcher.message, bot_dispatcher.callback_query):
        middlewares = observer.outer_middleware._middlewares
        assert any(isinstance(item, AuthorizationMiddleware) for item in middlewares)
        assert any(isinstance(item, OperationLogMiddleware) for item in middlewares)


async def test_bot_analysis_service_runs_without_gateway(db):
    """Bot submits with NO MTProto gateway; no inline execution happens."""
    service = AnalysisService()
    result = await service.submit("hello world test message", user_id=111)
    assert result.request_id is not None
    assert result.queued is False  # Redis unavailable in tests
    assert result.degraded is True
    # Nothing ran MTProto: the request stays QUEUED in the database.
    from app.database.engine import session_scope

    async with session_scope() as session:
        row = await session.get(AnalysisRequest, result.request_id)
        assert row is not None
        assert row.status == "queued"
    with pytest.raises(RuntimeError):
        await service.run_request(result.request_id)


async def test_submit_rejects_inline(db):
    service = AnalysisService()
    with pytest.raises(ValueError):
        await service.submit("x", 1, launch_inline=True)


async def test_recover_queued_reenqueues_stuck_requests(db, monkeypatch):
    from app.services import analysis_service

    service = AnalysisService()
    created = await service.submit("stuck request payload text", user_id=222)
    assert created.request_id is not None
    queued_ids: list[str] = []

    async def _fake_enqueue(name, *args, **kwargs):
        queued_ids.append(args[0])
        return True

    monkeypatch.setattr(analysis_service, "enqueue", _fake_enqueue)
    recovered = await service.recover_queued()
    assert recovered == 1
    assert queued_ids == [created.request_id]


# ------------------------------------------------------- API process isolation


async def test_api_never_creates_mtproto(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    calls: list[str] = []

    def _boom(*args, **kwargs):
        calls.append("create_gateway")
        raise AssertionError("API process must never create an MTProto gateway")

    async def _fake_enqueue(name, *args, **kwargs):
        return True

    monkeypatch.setattr("app.api.app.create_gateway", _boom, raising=False)
    monkeypatch.setattr("app.api.app.enqueue", _fake_enqueue)
    app = create_app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-admin-key-secret"}
    assert client.get("/api/v1/admin/chats", headers=headers).status_code == 200
    assert client.post(
        "/api/v1/admin/rules",
        json={"kind": "exact", "pattern": "zzz", "category": "test"},
        headers=headers,
    ).status_code == 200
    assert client.get("/health").status_code == 200
    assert calls == []


async def test_api_status_does_not_need_gateway(db):
    from app.services.status_service import collect_status

    status = await collect_status()
    assert status["database"] in ("ok", "error")
    assert status["redis"] == "error"
    assert status["mtproto"]["connected"] is False
    assert "last_connected" in status["mtproto"]


# ------------------------------------------------------- invite join safety


async def test_resolve_private_invite_never_joins():
    from telethon.tl.types import ChatInvite, PhotoEmpty

    invite = ChatInvite(
        title="secret group",
        photo=PhotoEmpty(id=1),
        participants_count=5,
        color=0,
        participants=[],
    )
    gateway = TelegramGateway(SessionStore())
    gateway._client = object()

    async def _resolve_invite_call(chat_id, factory):
        return invite

    async def _noop():
        pass

    gateway._call = _resolve_invite_call
    gateway.ensure_connected = _noop

    resolved = await gateway._resolve_invite("AAAAhash")
    assert resolved.access_state == AccessState.PRIVATE_NO_ACCESS
    assert resolved.error == "private_no_access"
    assert resolved.chat_id == 0


async def test_resolve_private_invite_already_joined_accesses():
    """ChatInviteAlready (member already) → ACCESSIBLE, still no joining."""
    from telethon.tl.types import Channel, ChatInviteAlready, ChatPhotoEmpty

    channel = Channel(
        id=123456789,
        title="Joined Channel",
        photo=ChatPhotoEmpty(),
        broadcast=True,
        date=await _utc_datetime(),
    )
    invite_already = ChatInviteAlready(chat=channel)
    gateway = TelegramGateway(SessionStore())
    gateway._client = object()

    async def _resolve_invite_call(chat_id, factory):
        return invite_already

    async def _noop():
        pass

    gateway._call = _resolve_invite_call
    gateway.ensure_connected = _noop

    resolved = await gateway._resolve_invite("AAAAhash")
    assert resolved.access_state == AccessState.ACCESSIBLE
    assert resolved.chat_id == -1000123456789  # channel id convention
    assert resolved.chat_type == "channel"


async def _utc_datetime():
    from datetime import UTC, datetime

    return datetime(2025, 1, 1, tzinfo=UTC)


async def test_resolve_invalid_invite_reports_invalid():
    gateway = TelegramGateway(SessionStore())
    gateway._client = object()

    async def _fail(chat_id, factory):
        raise TelegramAccessError("invite hash invalid", code="invalid_invite")

    async def _noop():
        pass

    gateway._call = _fail
    gateway.ensure_connected = _noop

    resolved = await gateway._resolve_invite("badhash")
    assert resolved.access_state == AccessState.INVALID_INVITE


# ------------------------------------------------------- flood / session errors


async def test_flood_wait_is_bounded_and_typed():
    from telethon.errors import FloodWaitError

    class _HugeFlood(FloodWaitError):
        def __init__(self, seconds: int):
            super().__init__(request=None)
            self.seconds = seconds

    settings = get_settings()
    gateway = TelegramGateway(SessionStore())
    gateway._client = object()

    async def _noop():
        pass

    gateway.ensure_connected = _noop

    async def _huge_flood(client):
        raise _HugeFlood(10_000)

    with pytest.raises(ChatFloodWaitError) as exc_info:
        await gateway._call(None, _huge_flood)
    assert exc_info.value.seconds == settings.mt_proto_max_flood_sleep
    assert exc_info.value.retryable is False

    async def _small_flood(client):
        raise _HugeFlood(30)

    with pytest.raises(ChatFloodWaitError) as exc_info:
        await gateway._call(None, _small_flood)
    assert exc_info.value.seconds == 30


def test_classify_session_expired():
    from telethon.errors import AuthKeyUnregisteredError, UnauthorizedError

    assert classify_access_error(AuthKeyUnregisteredError(request=None)) == (
        "session_expired",
        False,
    )
    assert classify_access_error(UnauthorizedError(request=None, message="unauthorized")) == (
        "session_expired",
        False,
    )
    assert classify_access_error(TimeoutError()) == ("network_error", True)


async def test_call_maps_session_expired_and_network():
    from telethon.errors import AuthKeyUnregisteredError

    gateway = TelegramGateway(SessionStore())
    gateway._client = object()

    async def _noop():
        pass

    gateway.ensure_connected = _noop

    async def _auth_fail(client):
        raise AuthKeyUnregisteredError(request=None)

    with pytest.raises(SessionExpiredError):
        await gateway._call(None, _auth_fail)

    async def _transport_fail(client):
        raise TimeoutError()

    with pytest.raises(NetworkError) as exc_info:
        await gateway._call(None, _transport_fail)
    assert exc_info.value.retryable is True


async def test_transient_error_retries_then_network_error(monkeypatch):
    gateway = TelegramGateway(SessionStore())
    gateway._client = object()

    async def _noop():
        pass

    gateway.ensure_connected = _noop
    monkeypatch.setattr("asyncio.sleep", lambda s: _noop())

    attempts = {"n": 0}

    async def _always_timeout(client):
        attempts["n"] += 1
        raise TimeoutError()

    settings = get_settings()
    old = settings.mt_proto_retry_limit
    settings.mt_proto_retry_limit = 3
    try:
        with pytest.raises(NetworkError):
            await gateway._call(None, _always_timeout)
    finally:
        settings.mt_proto_retry_limit = old
    assert attempts["n"] == 3


# ------------------------------------------------------- worker chat jobs


async def test_worker_add_chat_job_resolves_via_gateway(db, fake_gateway, monkeypatch):
    from app.workers import functions

    fake_gateway.chat_meta[CHAT_ID] = {
        "id": CHAT_ID, "title": "Public Group", "username": "publicgroup",
        "chat_type": "group",
    }
    monkeypatch.setattr(functions, "_get_gateway", lambda: fake_gateway)
    monkeypatch.setattr(functions, "_notify_user", _noop_notify)
    ctx: dict = {}

    result = await functions.add_chat(ctx, "@publicgroup", "123")
    assert result["ok"] is True
    assert result["chat_id"] == CHAT_ID

    chat_service = ChatService()
    chat = await chat_service.get_chat(CHAT_ID)
    assert chat is not None
    assert chat.username == "publicgroup"
    assert chat.access_state == AccessState.ACCESSIBLE.value


async def test_worker_add_chat_private_invite_refuses(db, fake_gateway, monkeypatch):
    """A private invite where the scanner is not a member must be refused."""
    from app.workers import functions

    monkeypatch.setattr(functions, "_get_gateway", lambda: fake_gateway)
    monkeypatch.setattr(functions, "_notify_user", _noop_notify)
    result = await functions.add_chat(ctx={}, reference="https://t.me/+abc123", actor="123")
    assert result["ok"] is False
    assert result["error"] == "private_no_access"

    from sqlalchemy import select

    from app.database.engine import session_scope
    from app.database.models import TargetChat

    async with session_scope() as session:
        rows = (await session.execute(select(TargetChat))).scalars().all()
    assert rows == []


async def test_worker_revoke_session_job(db, fake_gateway, monkeypatch):
    from app.workers import functions

    monkeypatch.setattr(functions, "_get_gateway", lambda: fake_gateway)
    result = await functions.revoke_session(ctx={})
    assert result["revoked"] is True

    from app.database.engine import session_scope

    async with session_scope() as session:
        state = await session.get(AppState, "telegram_session")
    assert state is not None
    assert state.value.get("revoked") is True


async def test_worker_check_chat_job_updates_access(db, fake_gateway, monkeypatch):
    from app.workers import functions

    fake_gateway.chat_meta[CHAT_ID] = {
        "id": CHAT_ID, "title": "Public Group", "username": "publicgroup",
        "chat_type": "group",
    }
    monkeypatch.setattr(functions, "_get_gateway", lambda: fake_gateway)
    monkeypatch.setattr(functions, "_notify_user", _noop_notify)
    await functions.add_chat(ctx={}, reference="@publicgroup", actor="api")
    result = await functions.check_chat(ctx={}, chat_id=CHAT_ID, actor="api")
    assert result["ok"] is True
    assert result["access_state"] == AccessState.ACCESSIBLE.value


# ---------------------------------------------------------------- pipeline


async def test_unseen_scan_skips_codes_mentions_and_urls():
    from app.analysis.normalize import normalize_document
    from app.analysis.pipeline import AnalysisPipeline

    pipeline = AnalysisPipeline.__new__(AnalysisPipeline)
    doc = normalize_document(
        "code #hash123 @mention example.com check https://x.io/ab"
    )
    findings: list[Finding] = []

    async def _noop(chat, doc, findings):
        pass

    pipeline._history_evidence = _noop
    pipeline._unseen_scan(None, doc, findings)
    assert findings == []


async def test_unseen_scan_keeps_real_novel_words():
    from app.analysis.normalize import normalize_document
    from app.analysis.pipeline import AnalysisPipeline

    pipeline = AnalysisPipeline.__new__(AnalysisPipeline)
    doc = normalize_document("the quasimodo protocol could change everything here")
    findings: list[Finding] = []

    async def _noop(chat, doc, findings):
        pass

    pipeline._history_evidence = _noop
    pipeline._unseen_scan(None, doc, findings)
    terms = {f.term for f in findings}
    assert "quasimodo" in terms
    assert all(f.evidence == EvidenceType.UNSEEN for f in findings)


# ------------------------------------------- worker-only MTProto (regressions)


def test_bot_and_api_modules_never_reference_gateway():
    """Source-level guarantee: bot/API wiring cannot initialize MTProto.

    No bot or API module may import/instantiate TelegramGateway, SessionStore
    or TelegramClient/telethon — and none may reference session material in
    any form (the worker owns the scanner session and its file). Worker
    modules are excluded: only the worker owns the scanner session.
    """
    import inspect

    import app.bot.bot_factory as factory_module
    import app.bot.handlers.chats as chats_module
    import app.bot.handlers.check as check_module
    import app.bot.handlers.commands as commands_module
    import app.main as main_module
    import app.services.chat_service as chat_service_module
    import app.services.status_service as status_module
    from app import api

    modules = (
        api.app,
        factory_module,
        check_module,
        chats_module,
        commands_module,
        main_module,
        chat_service_module,
        status_module,
    )
    source = "\n".join(inspect.getsource(m) for m in modules)
    for forbidden in (
        "TelegramGateway",
        "create_gateway",
        "SessionStore",
        "session_store",
        "TelegramClient",
        "telethon",
        "session.enc",
        "session_file",
        "session_enc",
        "master_secret",
        "MASTER_SECRET",
        "SESSION_FILE",
        "SESSION_ENC",
    ):
        assert forbidden not in source, (
            f"bot/API modules must not reference {forbidden!r} "
            "(worker owns MTProto + session material)"
        )


async def test_main_bot_service_wiring_constructs_without_gateway(db, monkeypatch, bot_dispatcher):
    """`python -m app.main bot` wiring: DB, dispatcher, services, API app,
    API server and polling all construct with NO MTProto gateway anywhere."""
    import uvicorn

    import app.main as main_module
    from app.bot import bot_factory

    async def _noop(*args, **kwargs):
        return None

    calls: list[str] = []

    async def _noop_polling(dispatcher, bot):
        calls.append("polling")
        return None

    # aiogram routers are already attached to `bot_dispatcher`; re-building
    # would raise. The real `build_dispatcher` is exercised once by the
    # `bot_dispatcher` fixture — here we prove main.py's wiring around it.
    monkeypatch.setattr(
        bot_factory, "build_dispatcher",
        lambda analysis, chat_service: (bot_dispatcher, object()),
    )
    monkeypatch.setattr(bot_factory, "start_bot_polling", _noop_polling)
    monkeypatch.setattr(uvicorn.Server, "serve", _noop)

    await main_module._run_bot_service()

    assert calls == ["polling"]


async def test_main_api_service_wiring_constructs_without_gateway(db, monkeypatch):
    """`python -m app.main api` wiring: DB + API app, no gateway."""
    import uvicorn

    import app.main as main_module

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", _noop)
    await main_module._run_api_service()


async def test_session_revoke_then_reprovision_transition(db, monkeypatch, tmp_path):
    """/logout → revoked flag set + file wiped + connect refused; a freshly
    provisioned session + unrevoke restores loadability (no worker restart)."""
    import app.telegram.session_store as session_store_module
    from app.config import get_settings
    from app.telegram.gateway import TelegramGateway
    from app.telegram.session_store import SessionStore

    session_file = tmp_path / "session.enc"
    patched = get_settings().model_copy(
        update={"session_file": str(session_file), "session_enc": None}
    )
    monkeypatch.setattr(session_store_module, "get_settings", lambda: patched)

    session_string_one = "1AAfake_session_string_one"
    session_string_two = "1AAfake_session_string_two"
    store = SessionStore()
    await store.save_new_session(session_string_one)
    assert await store.load() == session_string_one

    await store.revoke()
    assert await store.is_revoked() is True
    assert not session_file.exists()
    assert await store.load() is None

    # The worker refuses to connect while the session is revoked.
    gateway = TelegramGateway(store)
    assert await gateway.connect() is False

    # Re-provision: new session saved + flag cleared → loadable again.
    await store.save_new_session(session_string_two)
    await store.unrevoke()
    assert await store.is_revoked() is False
    fresh = SessionStore()
    assert await fresh.load() == session_string_two


# ------------------------------------------------ history sync stop conditions


async def test_sync_initial_caps_at_message_count_not_ids(db):
    """Initial sync must stop after N MESSAGES (not `cursor + N` as a
    message-ID threshold). Non-contiguous ids prove the count-based cap."""
    from datetime import UTC, datetime

    from app.config import get_settings
    from app.database.engine import session_scope
    from app.database.models import SyncState, TargetChat
    from app.history.indexer import HistoryIndexer
    from tests.conftest import FakeMessage

    ids = [1000 + i * 7 for i in range(9)]
    settings = get_settings()
    settings.initial_sync_max_messages = 5

    class _SparseGateway:
        def __init__(self) -> None:
            self.pages: list[int] = []

        async def estimate_total(self, chat_id: int) -> int:
            return 10_000  # deliberately far larger than any message id

        async def iter_messages(self, chat_id: int, *, min_id=None, limit=500, topic_id=None):
            self.pages.append(limit)
            page = [
                FakeMessage(message_id=i,
                    date=datetime(2025, 1, 1, tzinfo=UTC),
                    text=f"message body {i}",
                )
                for i in ids
                if i > (min_id or 0)
            ]
            return page[:limit]

    async with session_scope() as session:
        chat = TargetChat(
            telegram_chat_id=-100444001, title="cap test", chat_type="group"
        )
        session.add(chat)
        await session.flush()
        pk = chat.id

    from sqlalchemy import select

    async with session_scope() as session:
        chat = (await session.execute(select(TargetChat).where(TargetChat.id == pk))).scalar_one()

    gateway = _SparseGateway()
    report = await HistoryIndexer(gateway).sync_chat(chat, "initial")
    assert report.processed == 5, "cap must count messages, not compare ids"
    assert report.new_messages == 5
    assert report.end_reached is False
    assert gateway.pages == [5]

    async with session_scope() as session:
        row = await session.get(TargetChat, pk)
        assert row is not None
        assert row.sync_cursor == max(ids[:5])
        assert row.sync_state == SyncState.PARTIAL.value

    settings.initial_sync_max_messages = 200_000


async def test_sync_incremental_ignores_estimate_and_runs_to_end(db):
    """Incremental sync must run until history is exhausted; the estimate
    (a COUNT) must never be compared against message IDs (sparse ids far
    above the estimate prove it)."""
    from datetime import UTC, datetime

    from app.database.engine import session_scope
    from app.database.models import SyncState, TargetChat
    from app.history.indexer import HistoryIndexer
    from tests.conftest import FakeMessage

    ids = [1000 + i * 7 for i in range(9)]

    class _TinyGateway:
        def __init__(self) -> None:
            self.pages: list[int] = []

        async def estimate_total(self, chat_id: int) -> int:
            return 5  # count, far below every message id

        async def iter_messages(self, chat_id: int, *, min_id=None, limit=500, topic_id=None):
            self.pages.append(limit)
            page = [
                FakeMessage(message_id=i,
                    date=datetime(2025, 1, 1, tzinfo=UTC),
                    text=f"message body {i}",
                )
                for i in ids
                if i > (min_id or 0)
            ]
            return page[:limit]

    async with session_scope() as session:
        chat = TargetChat(
            telegram_chat_id=-100444002, title="run-to-end test", chat_type="group"
        )
        session.add(chat)
        await session.flush()
        pk = chat.id

    from sqlalchemy import select

    async with session_scope() as session:
        chat = (await session.execute(select(TargetChat).where(TargetChat.id == pk))).scalar_one()

    gateway = _TinyGateway()
    report = await HistoryIndexer(gateway).sync_chat(chat, "incremental")
    assert report.processed == len(ids)
    assert report.end_reached is True

    async with session_scope() as session:
        row = await session.get(TargetChat, pk)
        assert row is not None
        assert row.sync_cursor == ids[-1]
        assert row.sync_state == SyncState.DONE.value


CHAT_ID = -100777
