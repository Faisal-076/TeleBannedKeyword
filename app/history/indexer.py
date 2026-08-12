"""Incremental history indexing into PostgreSQL.

- initial_sync : full backfill up to INITIAL_SYNC_MAX_MESSAGES (paginated)
- incremental_sync : from the stored cursor forward
- manual_resync : initial mode forced from cursor 0

Stores only analysis-relevant data: message id, date, trimmed text, hash,
normalized text, extracted terms, and per-term aggregates with a small
context sample. Never downloads media.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.analysis.normalize import normalize_document
from app.analysis.tokenize import extract_terms
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import (
    CONTEXT_SNIPPET_MAX,
    INDEXED_TEXT_MAX,
    IndexedMessage,
    MessageTerm,
    PhraseOccurrence,
    SyncState,
    TargetChat,
)
from app.history.coverage import compute_coverage
from app.telegram.errors import TelegramAccessError
from app.telegram.gateway import TelegramGateway
from app.utils.hash import sha256_hex
from app.utils.timeutil import utc_now_naive

logger = logging.getLogger("app.history.indexer")


@dataclass
class SyncReport:
    chat_id: int
    mode: str
    processed: int = 0
    new_messages: int = 0
    end_reached: bool = False
    estimate: int | None = None
    error: str | None = None


class HistoryIndexer:
    def __init__(self, gateway: TelegramGateway):
        self._gateway = gateway
        self._settings = get_settings()

    async def sync_chat(self, chat: TargetChat, mode: str) -> SyncReport:
        assert mode in ("initial", "incremental", "resync")
        if mode == "resync":
            chat.sync_cursor = None
            chat.sync_indexed_count = 0
            await self._persist_cursor(chat, 0)
        report = SyncReport(chat_id=chat.telegram_chat_id, mode=mode)
        await self._persist_running(chat)

        try:
            estimate = await self._gateway.estimate_total(chat.telegram_chat_id)
            report.estimate = estimate
            await self._persist_estimate(chat, estimate)

            limit_total = self._settings.initial_sync_max_messages
            if mode == "incremental":
                limit_total = 0
            cursor = chat.sync_cursor or 0
            max_id = cursor + limit_total if limit_total else None

            while True:
                page = await self._gateway.iter_messages(
                    chat.telegram_chat_id,
                    min_id=cursor,
                    limit=self._settings.incremental_sync_batch,
                    topic_id=chat.topic_id,
                )
                if not page:
                    report.end_reached = True
                    break
                created = await self._store_page(chat, page)
                report.processed += len(page)
                report.new_messages += created
                cursor = max(h.message_id for h in page)
                await self._persist_cursor(chat, cursor)
                if max_id and cursor >= max_id:
                    break
                if max_id is None and estimate and cursor >= estimate:
                    break

            await self._persist_done(chat)
            logger.info(
                "history: sync complete chat=%s mode=%s processed=%d new=%d",
                chat.telegram_chat_id, mode, report.processed, report.new_messages,
            )
            if not report.end_reached:
                coverage = compute_coverage(chat)
                chat.sync_state = SyncState.PARTIAL.value
                chat.sync_error = coverage.note
                report.error = coverage.note
            return report
        except TelegramAccessError as exc:
            chat.sync_state = SyncState.FAILED.value
            chat.sync_error = exc.code
            report.error = exc.code
            await self._persist_failed(chat, exc.code)
            logger.warning("history: sync failed chat=%s code=%s", chat.telegram_chat_id, exc.code)
            return report
        except Exception as exc:  # noqa: BLE001
            chat.sync_state = SyncState.FAILED.value
            chat.sync_error = type(exc).__name__
            report.error = type(exc).__name__
            await self._persist_failed(chat, type(exc).__name__)
            logger.error("history: sync crashed chat=%s err=%s", chat.telegram_chat_id, type(exc).__name__)
            return report

    async def _store_page(self, chat: TargetChat, page) -> int:
        created = 0
        seen_term_counts: dict[str, int] = {}
        seen_samples: dict[str, str] = {}
        async with session_scope() as session:
            for hit in page:
                if not hit.text.strip():
                    continue
                text_trimmed = hit.text[:INDEXED_TEXT_MAX]
                text_hash = sha256_hex(text_trimmed)
                existing = await session.execute(
                    select(IndexedMessage).where(
                        IndexedMessage.chat_id == chat.telegram_chat_id,
                        IndexedMessage.message_id == hit.message_id,
                    )
                )
                row = existing.scalar_one_or_none()
                normalized = normalize_document(text_trimmed).clean
                if row is None:
                    session.add(
                        IndexedMessage(
                            chat_id=chat.telegram_chat_id,
                            message_id=hit.message_id,
                            date=hit.date,
                            topic_id=hit.topic_id,
                            text_hash=text_hash,
                            text=text_trimmed,
                            normalized_text=normalized,
                        )
                    )
                    created += 1
                else:
                    row.text_hash = text_hash
                    row.text = text_trimmed
                    row.normalized_text = normalized
                    row.date = hit.date
                terms = extract_terms(normalized, max_n=2)
                for term in terms:
                    seen_term_counts[term] = seen_term_counts.get(term, 0) + 1
                    seen_samples.setdefault(term, _context_snippet(text_trimmed, term))
                    session.add(
                        MessageTerm(
                            chat_id=chat.telegram_chat_id,
                            term=term,
                            message_id=hit.message_id,
                        )
                    )
            # update aggregated phrase counts
            for term, count in seen_term_counts.items():
                existing = await session.execute(
                    select(PhraseOccurrence).where(
                        PhraseOccurrence.chat_id == chat.telegram_chat_id,
                        PhraseOccurrence.term == term,
                    )
                )
                agg = existing.scalar_one_or_none()
                now = utc_now_naive()
                if agg is None:
                    session.add(
                        PhraseOccurrence(
                            chat_id=chat.telegram_chat_id,
                            term=term,
                            count=count,
                            first_seen_at=now,
                            last_seen_at=now,
                            sample_message_id=page[0].message_id,
                            sample_context=seen_samples.get(term, "")[:CONTEXT_SNIPPET_MAX],
                        )
                    )
                else:
                    agg.count += count
                    agg.last_seen_at = now
                    if agg.sample_context is None:
                        agg.sample_context = seen_samples.get(term, "")[:CONTEXT_SNIPPET_MAX]
        return created

    async def _persist_cursor(self, chat: TargetChat, cursor: int) -> None:
        async with session_scope() as session:
            row = await session.get(TargetChat, chat.id)
            if row is not None:
                row.sync_cursor = cursor
                row.sync_at = utc_now_naive()

    async def _persist_running(self, chat: TargetChat) -> None:
        async with session_scope() as session:
            row = await session.get(TargetChat, chat.id)
            if row is not None:
                row.sync_state = SyncState.RUNNING.value
                row.sync_at = utc_now_naive()

    async def _persist_failed(self, chat: TargetChat, error: str) -> None:
        async with session_scope() as session:
            row = await session.get(TargetChat, chat.id)
            if row is not None:
                row.sync_state = SyncState.FAILED.value
                row.sync_error = error
                row.sync_at = utc_now_naive()

    async def _persist_estimate(self, chat: TargetChat, estimate: int | None) -> None:
        async with session_scope() as session:
            row = await session.get(TargetChat, chat.id)
            if row is not None:
                row.sync_estimate = estimate

    async def _persist_done(self, chat: TargetChat) -> None:
        async with session_scope() as session:
            row = await session.get(TargetChat, chat.id)
            if row is not None:
                row.sync_state = SyncState.DONE.value
                row.sync_at = utc_now_naive()
                count_result = await session.execute(
                    select(IndexedMessage.id).where(IndexedMessage.chat_id == row.telegram_chat_id)
                )
                row.sync_indexed_count = len(list(count_result.all()))


def _context_snippet(text: str, term: str) -> str:
    idx = text.casefold().find(term)
    if idx < 0:
        return text[:CONTEXT_SNIPPET_MAX]
    start = max(0, idx - 100)
    snippet = text[start : start + CONTEXT_SNIPPET_MAX].strip()
    if start > 0:
        snippet = "..." + snippet
    return snippet
