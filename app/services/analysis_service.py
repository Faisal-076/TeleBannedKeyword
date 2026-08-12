"""Analysis request lifecycle: submit → queue → run → store → retrieve.

- Submissions are never forwarded to any Telegram chat (analysis-only).
- Jobs are idempotent: re-running the same request id is a no-op once DONE.
- If Redis is down, jobs run inline as background tasks (degraded mode).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.analysis.models import AnalysisOutcome
from app.analysis.normalize import normalize_document
from app.analysis.pipeline import AnalysisPipeline, PipelineDeps
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import AnalysisRequest, AnalysisResult, AnalysisStatus
from app.history.searcher import HistorySearcher
from app.llm.providers import create_provider
from app.services.queue import enqueue
from app.telegram.gateway import TelegramGateway
from app.utils.ids import new_analysis_id
from app.utils.timeutil import utc_now_naive

logger = logging.getLogger("app.services.analysis")


@dataclass
class SubmitResult:
    request_id: str | None
    error: str | None = None
    queued: bool = False
    degraded: bool = False


class AnalysisService:
    def __init__(self, gateway: TelegramGateway) -> None:
        self._gateway = gateway
        self._settings = get_settings()
        self._inline_tasks: set[asyncio.Task] = set()

    async def submit(
        self,
        text: str,
        user_id: int,
        *,
        launch_inline: bool = True,
    ) -> SubmitResult:
        if not text or not text.strip():
            return SubmitResult(None, "empty message")
        if len(text) > self._settings.max_message_chars:
            return SubmitResult(
                None,
                f"message too large ({len(text)} chars); max is "
                f"{self._settings.max_message_chars}. Split it or shorten it — "
                "analysis is never silently truncated.",
            )
        doc = normalize_document(text)
        request_id = new_analysis_id()
        async with session_scope() as session:
            session.add(
                AnalysisRequest(
                    id=request_id,
                    user_id=user_id,
                    original_text=text,
                    normalized_text=doc.clean,
                    status=AnalysisStatus.QUEUED.value,
                )
            )
        queued = await enqueue("analyze_message", request_id, job_id=request_id)
        if queued:
            return SubmitResult(request_id=request_id, queued=True)
        if not launch_inline:
            return SubmitResult(request_id=request_id, queued=False, degraded=True)
        # degraded mode: run inline
        task = asyncio.create_task(self._run_wrapped(request_id))
        self._inline_tasks.add(task)
        task.add_done_callback(self._inline_tasks.discard)
        return SubmitResult(request_id=request_id, queued=False, degraded=True)

    async def _run_wrapped(self, request_id: str) -> None:
        try:
            await self.run_request(request_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("analysis: inline job failed request=%s err=%s", request_id, type(exc).__name__)

    async def run_request(self, request_id: str) -> AnalysisOutcome | None:
        """Execute one analysis job (worker entry point). Idempotent."""
        async with session_scope() as session:
            request = await session.get(AnalysisRequest, request_id)
            if request is None:
                return None
            if request.status == AnalysisStatus.DONE.value:
                existing = await session.execute(
                    select(AnalysisResult).where(AnalysisResult.request_id == request_id)
                )
                if existing.scalar_one_or_none() is not None:
                    return None
            if request.status == AnalysisStatus.RUNNING.value:
                return None
            request.status = AnalysisStatus.RUNNING.value
            original = request.original_text
            user_id = request.user_id

        llm = create_provider(self._settings)
        searcher = HistorySearcher(self._gateway)
        pipeline = AnalysisPipeline(
            PipelineDeps(gateway=self._gateway, searcher=searcher, llm=llm)
        )
        try:
            outcome = await pipeline.analyze(original)
            outcome.request_id = request_id
            storage = outcome.to_storage()
            async with session_scope() as session:
                request = await session.get(AnalysisRequest, request_id)
                if request is None:
                    return None
                try:
                    existing = await session.execute(
                        select(AnalysisResult).where(AnalysisResult.request_id == request_id)
                    )
                    result_row = existing.scalar_one_or_none()
                    if result_row is None:
                        result_row = AnalysisResult(request_id=request_id)
                        session.add(result_row)
                    result_row.overall_score = storage["overall_score"]
                    result_row.overall_level = storage["overall_level"]
                    result_row.global_result = storage["global_result"]
                    result_row.chat_results = storage["chat_results"]
                    request.status = AnalysisStatus.DONE.value
                    request.completed_at = utc_now_naive()
                except IntegrityError:
                    # another concurrent runner (inline fallback + worker)
                    # persisted this request first; theirs wins.
                    return None
            logger.info(
                "analysis: done request=%s user=%s score=%s",
                request_id, user_id, outcome.overall_score,
            )
            return outcome
        except Exception as exc:  # noqa: BLE001
            logger.error("analysis: failed request=%s err=%s", request_id, type(exc).__name__)
            async with session_scope() as session:
                request = await session.get(AnalysisRequest, request_id)
                if request is not None:
                    request.status = AnalysisStatus.FAILED.value
                    request.error = type(exc).__name__
            return None

    async def get_outcome(self, request_id: str) -> AnalysisOutcome | None:
        async with session_scope() as session:
            result = await session.execute(
                select(AnalysisResult, AnalysisRequest)
                .join(AnalysisRequest, AnalysisResult.request_id == AnalysisRequest.id)
                .where(AnalysisResult.request_id == request_id)
            )
            pair = result.one_or_none()
            if pair is None:
                return None
            row, request_row = pair
            return AnalysisOutcome(
                request_id=request_id,
                original_text=request_row.original_text,
                normalized_text=request_row.normalized_text,
                overall_score=row.overall_score,
                overall_level=row.overall_level,
                chat_results=row.chat_results or [],
                global_summary=(row.global_result or {}).get("summary", []),
                global_recommendations=(row.global_result or {}).get("recommendations", []),
                ai_used=bool((row.global_result or {}).get("ai_used")),
                ai_confidence=(row.global_result or {}).get("ai_confidence"),
                warnings=(row.global_result or {}).get("warnings", []),
            )

    async def get_status(self, request_id: str) -> tuple[str, str | None]:
        async with session_scope() as session:
            request = await session.get(AnalysisRequest, request_id)
            if request is None:
                return "not_found", None
            return request.status, request.error

    async def get_request_user(self, request_id: str) -> int | None:
        async with session_scope() as session:
            request = await session.get(AnalysisRequest, request_id)
            return request.user_id if request else None

    async def recheck(
        self,
        request_id: str,
        new_text: str,
        *,
        launch_inline: bool = True,
    ) -> SubmitResult:
        """Re-analyze the same request id with edited text."""
        if len(new_text) > self._settings.max_message_chars:
            return SubmitResult(
                None,
                f"message too large ({len(new_text)} chars); max is {self._settings.max_message_chars}.",
            )
        async with session_scope() as session:
            request = await session.get(AnalysisRequest, request_id)
            if request is None:
                return SubmitResult(None, "request not found")
            request.original_text = new_text
            request.normalized_text = normalize_document(new_text).clean
            request.status = AnalysisStatus.QUEUED.value
            request.completed_at = None
            request.retry_count += 1
            result = await session.execute(
                select(AnalysisResult).where(AnalysisResult.request_id == request_id)
            )
            old = result.scalar_one_or_none()
            if old is not None:
                await session.delete(old)
        queued = await enqueue("analyze_message", request_id, job_id=f"{request_id}:{request.retry_count}")
        if queued:
            return SubmitResult(request_id=request_id, queued=True)
        if not launch_inline:
            return SubmitResult(request_id=request_id, queued=False, degraded=True)
        task = asyncio.create_task(self._run_wrapped(request_id))
        self._inline_tasks.add(task)
        task.add_done_callback(self._inline_tasks.discard)
        return SubmitResult(request_id=request_id, queued=False, degraded=True)
