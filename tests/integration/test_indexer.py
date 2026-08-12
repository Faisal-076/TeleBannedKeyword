from sqlalchemy import func, select

from app.database.engine import session_scope
from app.database.models import (
    IndexedMessage,
    MessageTerm,
    PhraseOccurrence,
    SyncState,
    TargetChat,
)
from app.history.coverage import compute_coverage
from app.history.indexer import HistoryIndexer
from tests.conftest import make_message


async def _seed_chat(chat_id: int = -100123, **fields) -> TargetChat:
    async with session_scope() as session:
        chat = TargetChat(
            telegram_chat_id=chat_id,
            title="Test Chat",
            access_state="accessible",
            **fields,
        )
        session.add(chat)
        await session.flush()
        return chat


async def test_initial_sync_indexes_messages(db, fake_gateway):
    chat = await _seed_chat()
    fake_gateway.add_messages(
        chat.telegram_chat_id,
        [
            make_message(chat.telegram_chat_id, 1, "hello world"),
            make_message(chat.telegram_chat_id, 2, "free bitcoin giveaway"),
            make_message(chat.telegram_chat_id, 3, "hello again"),
        ],
    )
    indexer = HistoryIndexer(fake_gateway)
    report = await indexer.sync_chat(chat, "initial")
    assert report.processed == 3
    assert report.end_reached is True

    async with session_scope() as session:
        count = (await session.execute(select(func.count(IndexedMessage.id)))).scalar()
        assert count == 3
        terms = (await session.execute(select(func.count(MessageTerm.id)))).scalar()
        assert terms > 0
        occ = (
            await session.execute(
                select(PhraseOccurrence).where(PhraseOccurrence.term == "bitcoin")
            )
        ).scalar_one_or_none()
        assert occ is not None and occ.count == 1
        hello_occ = (
            await session.execute(
                select(PhraseOccurrence).where(PhraseOccurrence.term == "hello")
            )
        ).scalar_one_or_none()
        assert hello_occ is not None and hello_occ.count == 2

        fresh = await session.get(TargetChat, chat.id)
        assert fresh.sync_state == SyncState.DONE.value
        assert fresh.sync_cursor == 3
        assert fresh.sync_indexed_count == 3


async def test_incremental_sync_only_new_messages(db, fake_gateway):
    chat = await _seed_chat(sync_cursor=2, sync_state=SyncState.DONE.value)
    fake_gateway.add_messages(
        chat.telegram_chat_id,
        [
            make_message(chat.telegram_chat_id, 1, "old one"),
            make_message(chat.telegram_chat_id, 2, "old two"),
            make_message(chat.telegram_chat_id, 3, "brand new message"),
            make_message(chat.telegram_chat_id, 4, "another new message"),
        ],
    )
    indexer = HistoryIndexer(fake_gateway)
    report = await indexer.sync_chat(chat, "incremental")
    assert report.processed == 2
    assert report.new_messages == 2

    async with session_scope() as session:
        fresh = await session.get(TargetChat, chat.id)
        assert fresh.sync_cursor == 4


async def test_sync_marks_failed_on_access_error(db, fake_gateway):
    chat = await _seed_chat()
    fake_gateway.raise_on_iter = Exception("boom")

    from app.telegram.errors import TelegramAccessError

    fake_gateway.raise_on_iter = TelegramAccessError("no access", code="private_no_access")
    indexer = HistoryIndexer(fake_gateway)
    report = await indexer.sync_chat(chat, "initial")
    assert report.error == "private_no_access"

    async with session_scope() as session:
        fresh = await session.get(TargetChat, chat.id)
        assert fresh.sync_state == SyncState.FAILED.value


async def test_topic_scoped_sync(db, fake_gateway):
    chat = await _seed_chat(topic_id=77)
    fake_gateway.add_messages(
        chat.telegram_chat_id,
        [
            make_message(chat.telegram_chat_id, 1, "topic message", topic_id=77),
            make_message(chat.telegram_chat_id, 2, "general message", topic_id=None),
        ],
    )
    indexer = HistoryIndexer(fake_gateway)
    report = await indexer.sync_chat(chat, "initial")
    assert report.processed == 1


async def test_coverage_states(db):
    chat = await _seed_chat(sync_state=SyncState.DONE.value, sync_estimate=10, sync_indexed_count=9)
    coverage = compute_coverage(chat)
    assert coverage.is_complete

    chat2 = await _seed_chat(chat_id=-100222, sync_state=SyncState.PARTIAL.value, sync_estimate=1000, sync_indexed_count=50)
    coverage2 = compute_coverage(chat2)
    assert coverage2.state == "partial"
    assert "incomplete" in (coverage2.note or "")

    chat3 = await _seed_chat(chat_id=-100333, sync_state=SyncState.NONE.value)
    assert compute_coverage(chat3).state == "unknown"
