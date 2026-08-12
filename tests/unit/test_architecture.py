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


async def test_bot_dispatcher_has_no_gateway():
    from app.bot.bot_factory import build_dispatcher

    dispatcher, bot = build_dispatcher(
        AnalysisService(), ChatService(), SessionStore()
    )
    assert bot is None or bot.token  # just must construct
    assert "gateway" not in dispatcher.workflow_data
    assert "analysis" in dispatcher.workflow_data
    assert "chats" in dispatcher.workflow_data


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
    app = create_app(gateway=None)
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

    status = await collect_status(None)
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
    from telethon.errors import AuthKeyUnregisteredError, UnauthorizedError

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

    from app.database.engine import session_scope
    from app.database.models import TargetChat
    from sqlalchemy import select

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

    deps = object()
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


CHAT_ID = -100777