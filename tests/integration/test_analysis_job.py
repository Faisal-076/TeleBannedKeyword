"""End-to-end analysis job tests through the real AnalysisService (SQLite)."""

from sqlalchemy import select

from app.database.engine import session_scope
from app.database.models import (
    AnalysisRequest,
    AnalysisResult,
    Rule,
    SyncState,
    TargetChat,
)
from app.rules.repository import create_rule
from app.services.analysis_service import AnalysisService
from app.telegram.errors import TelegramAccessError
from tests.conftest import make_message

CHAT_A = -1001001
CHAT_B = -1001002


async def _seed_chat(chat_id: int, *, sync: bool = True, access: str = "accessible") -> TargetChat:
    async with session_scope() as session:
        chat = TargetChat(
            telegram_chat_id=chat_id,
            title=f"Chat {chat_id}",
            username=f"chat{abs(chat_id)}",
            chat_type="group",
            access_state=access,
            sync_state=SyncState.DONE.value if sync else SyncState.NONE.value,
            sync_estimate=3 if sync else None,
            sync_indexed_count=3 if sync else 0,
            sync_cursor=3 if sync else None,
        )
        session.add(chat)
        await session.flush()
        return chat


async def _seed_history(fake_gateway, chat_id: int):
    fake_gateway.add_messages(
        chat_id,
        [
            make_message(chat_id, 1, "welcome to our community"),
            make_message(chat_id, 2, "check out the weekly deal"),
            make_message(chat_id, 3, "everyone says welcome to everyone"),
        ],
    )


async def _run(text: str, fake_gateway) -> dict:
    service = AnalysisService(fake_gateway)
    result = await service.submit(text, user_id=111, launch_inline=False)
    assert result.request_id is not None
    await service.run_request(result.request_id)
    outcome = await service.get_outcome(result.request_id)
    assert outcome is not None
    return {
        "outcome": outcome,
        "request_id": result.request_id,
    }


async def test_normal_message_low_risk(db, fake_gateway):
    await _seed_chat(CHAT_A)
    await _seed_history(fake_gateway, CHAT_A)
    res = await _run("hello everyone, welcome to the group", fake_gateway)
    outcome = res["outcome"]
    assert outcome.overall_score < 40
    assert outcome.overall_level.value in ("LOW", "MODERATE")


async def test_explicit_banned_regex_high_risk(db, fake_gateway):
    await _seed_chat(CHAT_A)
    await _seed_history(fake_gateway, CHAT_A)
    await create_rule(
        scope="global", kind="regex",
        pattern=r"\b(free|get)\s+bitcoin\b", category="scam",
    )
    res = await _run("Get free bitcoin now!!!", fake_gateway)
    outcome = res["outcome"]
    assert outcome.overall_score >= 70
    assert outcome.overall_level.value in ("HIGH", "VERY HIGH")
    chat = outcome.chat_results[0]
    finding = next(f for f in chat.findings if f.evidence.value == "regex")
    assert finding is not None


async def test_unseen_phrase_not_banned(db, fake_gateway):
    """TEST 6: phrase never observed → UNSEEN, never 'banned'."""
    await _seed_chat(CHAT_A)
    await _seed_history(fake_gateway, CHAT_A)
    res = await _run("please consider the zorblax protocol proposal", fake_gateway)
    outcome = res["outcome"]
    chat = outcome.chat_results[0]
    unseen = [f for f in chat.findings if f.evidence.value == "unseen"]
    assert unseen, "expected an UNSEEN finding for novel wording"
    for finding in unseen:
        assert finding.risk < 40  # small weight only
        assert "Not observed" in finding.reason


async def test_historically_used_phrase_seen(db, fake_gateway):
    """TEST 7: phrase used frequently historically → SEEN evidence shown."""
    await _seed_chat(CHAT_A)
    fake_gateway.add_messages(
        CHAT_A,
        [make_message(CHAT_A, i, f"this channel offers special deals number {i}") for i in range(1, 20)],
    )
    res = await _run("special", fake_gateway)
    outcome = res["outcome"]
    chat = outcome.chat_results[0]
    seen = [f for f in chat.findings if f.history.state.value == "seen"]
    assert seen, "expected seen-history evidence"
    assert seen[0].history.count > 0


async def test_explicit_rule_beats_history(db, fake_gateway):
    """TEST 8: explicit banned rule + frequent historical use → still HIGH."""
    await _seed_chat(CHAT_A)
    fake_gateway.add_messages(
        CHAT_A,
        [make_message(CHAT_A, i, "this channel sells bannedword every day") for i in range(1, 15)],
    )
    await create_rule(scope="global", kind="exact", pattern="bannedword", category="spam")
    res = await _run("I also sell bannedword here", fake_gateway)
    outcome = res["outcome"]
    assert outcome.overall_score >= 70
    chat = outcome.chat_results[0]
    finding = next(f for f in chat.findings if f.term == "bannedword")
    assert finding.history.state.value == "seen"
    assert finding.history.count >= 10
    assert finding.risk >= 70
    assert any("rule overrides" in c for c in finding.score_components)


async def test_inaccessible_chat_degrades_other_chats(db, fake_gateway):
    """TEST 9: one unavailable chat → UNKNOWN/ERROR; others still analyzed."""
    await _seed_chat(CHAT_A)
    await _seed_chat(CHAT_B)
    await _seed_history(fake_gateway, CHAT_A)
    await _seed_history(fake_gateway, CHAT_B)
    fake_gateway.raise_on_search = TelegramAccessError(
        "banned", code="private_no_access"
    )

    await create_rule(scope="global", kind="exact", pattern="forbidden", category="spam")
    res = await _run("this is forbidden content", fake_gateway)
    outcome = res["outcome"]
    statuses = {c.chat_id: c.status.value for c in outcome.chat_results}
    assert len(outcome.chat_results) == 2
    assert "ok" in statuses.values(), "at least one chat analyzed"
    assert outcome.overall_score >= 70, "accessible chat still flags the banned word"


async def test_unindexed_chat_unknown_history(db, fake_gateway):
    await _seed_chat(CHAT_A, sync=False)
    fake_gateway.add_messages(CHAT_A, [make_message(CHAT_A, 1, "some historical content")])
    res = await _run("extremely unusual word here", fake_gateway)
    outcome = res["outcome"]
    chat = outcome.chat_results[0]
    assert chat.coverage_state == "unknown"
    assert all(f.history.state.value != "unseen" for f in chat.findings)


async def test_request_persisted_and_idempotent(db, fake_gateway):
    service = AnalysisService(fake_gateway)
    result = await service.submit("hello", user_id=111, launch_inline=False)
    assert result.request_id is not None
    first = await service.run_request(result.request_id)
    second = await service.run_request(result.request_id)
    assert first is not None
    assert second is None  # idempotent: already DONE

    async with session_scope() as session:
        request = await session.get(AnalysisRequest, result.request_id)
        assert request is not None and request.status == "done"
        row = (await session.execute(
            select(AnalysisResult).where(AnalysisResult.request_id == result.request_id)
        )).scalar_one_or_none()
        assert row is not None
        assert row.chat_results == []


async def test_oversized_message_rejected(db, fake_gateway):
    from app.config import get_settings

    service = AnalysisService(fake_gateway)
    big = "x" * (get_settings().max_message_chars + 1)
    result = await service.submit(big, user_id=111, launch_inline=False)
    assert result.request_id is None
    assert "too large" in (result.error or "")


async def test_rule_import_bulk(db):
    from app.rules.repository import import_rules_bulk

    created = await import_rules_bulk(
        [
            {"kind": "exact", "pattern": "scam", "category": "scam"},
            {"kind": "regex", "pattern": r"\bpv\b", "category": "promo"},
            {"kind": "phrase", "pattern": "click here", "allow": True},
        ]
    )
    assert created == 3
