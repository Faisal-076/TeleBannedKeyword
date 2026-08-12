"""Evidence gathering: is a term seen / unseen / unknown in a chat's history?

Logic (per specification):
- SEEN   : observed historically (local index or a successful Telegram search)
- UNSEEN : not observed AND history coverage is complete (searchable)
- UNKNOWN: history is incomplete/unavailable, or the search could not run

A previously observed term lowers risk but never overrides explicit rules.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.analysis.models import HistoryEvidence, HistoryState
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import CONTEXT_SNIPPET_MAX, PhraseOccurrence, TargetChat
from app.history.coverage import compute_coverage
from app.telegram.gateway import MessageHit, TelegramGateway

logger = logging.getLogger("app.history.searcher")


@dataclass
class HistorySearcher:
    gateway: TelegramGateway
    _freq_cache: dict[tuple[int, str], list[tuple[str, int]]] = field(default_factory=dict)
    _freq_cache_ts: dict[tuple[int, str], float] = field(default_factory=dict)

    async def term_occurrence(self, chat_id: int, term: str) -> PhraseOccurrence | None:
        async with session_scope() as session:
            result = await session.execute(
                select(PhraseOccurrence).where(
                    PhraseOccurrence.chat_id == chat_id,
                    PhraseOccurrence.term == term.casefold(),
                )
            )
            return result.scalar_one_or_none()

    async def _telegram_search(self, chat: TargetChat, term: str) -> list[MessageHit]:
        try:
            return await self.gateway.search_messages(
                chat.telegram_chat_id, term, limit=get_settings().history_search_limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("history: telegram search failed chat=%s term_len=%d err=%s",
                        chat.telegram_chat_id, len(term), type(exc).__name__)
            return []

    async def evidence_for_term(
        self,
        chat: TargetChat,
        term: str,
        *,
        fuzzy_candidates: bool = True,
    ) -> tuple[HistoryEvidence, str | None]:
        """Return (evidence, fuzzy_matched_term_or_None)."""
        term_key = term.casefold()
        coverage = compute_coverage(chat)
        local = await self.term_occurrence(chat.telegram_chat_id, term_key)

        if local is not None and local.count > 0:
            return (
                HistoryEvidence(
                    state=HistoryState.SEEN,
                    count=local.count,
                    example_context=local.sample_context,
                    note="observed in indexed history",
                ),
                None,
            )

        # Not in the local index → try a targeted Telegram search.
        hits = await self._telegram_search(chat, term_key)
        if hits:
            return (
                HistoryEvidence(
                    state=HistoryState.SEEN,
                    count=len(hits),
                    example_context=_snippet(hits[0].text, term_key),
                    note="found via Telegram message search",
                ),
                None,
            )

        if fuzzy_candidates:
            best = await self._fuzzy_history_match(chat.telegram_chat_id, term_key)
            if best is not None:
                return (
                    HistoryEvidence(
                        state=HistoryState.SEEN,
                        count=0,
                        example_context=None,
                        note=f"similar wording observed historically: {best[0]!r} ({best[1]}x)",
                    ),
                    best[0],
                )

        if coverage.is_complete:
            return (
                HistoryEvidence(
                    state=HistoryState.UNSEEN,
                    count=0,
                    note="not observed in the indexed/searchable history",
                ),
                None,
            )
        return (
            HistoryEvidence(
                state=HistoryState.UNKNOWN,
                count=0,
                note=coverage.note or "historical coverage incomplete",
            ),
            None,
        )

    async def _fuzzy_history_match(self, chat_id: int, term: str) -> tuple[str, int] | None:
        """Fuzzy match a term against the most frequent historical terms."""
        if len(term) < 5:
            return None
        freq = await self._frequent_terms(chat_id, 500)
        if not freq:
            return None
        best: tuple[str, int] | None = None
        best_score = 0.0
        for candidate, count in freq:
            if candidate == term:
                continue
            sim = _fast_sim(term, candidate)
            if sim > best_score:
                best_score = sim
                best = (candidate, count)
        if best and best_score >= get_settings().fuzzy_threshold:
            return best
        return None

    async def _frequent_terms(self, chat_id: int, limit: int) -> list[tuple[str, int]]:
        key = (chat_id, "freq")
        now = asyncio.get_running_loop().time()
        cached = self._freq_cache.get(key)
        if cached and now - self._freq_cache_ts.get(key, 0) < 600:
            return cached
        async with session_scope() as session:
            result = await session.execute(
                select(PhraseOccurrence.term, PhraseOccurrence.count)
                .where(PhraseOccurrence.chat_id == chat_id)
                .order_by(PhraseOccurrence.count.desc())
                .limit(limit)
            )
            rows = [(t, c) for t, c in result.all() if len(t) >= 5]
        self._freq_cache[key] = rows
        self._freq_cache_ts[key] = now
        return rows


def _snippet(text: str, term: str) -> str:
    idx = text.casefold().find(term)
    if idx < 0:
        idx = 0
    start = max(0, idx - 120)
    snippet = text[start : start + CONTEXT_SNIPPET_MAX].strip()
    if start > 0:
        snippet = "..." + snippet
    if start + CONTEXT_SNIPPET_MAX < len(text):
        snippet = snippet + "..."
    return snippet


def _fast_sim(a: str, b: str) -> float:
    from rapidfuzz import fuzz

    return fuzz.ratio(a, b) / 100.0
