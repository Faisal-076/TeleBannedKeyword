"""Analysis request lifecycle: submit → queue → run (worker) → store → retrieve.

- Submissions are never forwarded to any Telegram chat (analysis-only).
- Jobs are idempotent: re-running the same request id is a no-op once DONE.
- The bot NEVER executes analysis itself. If Redis is down the request stays
  queued in the database; the worker's `recover_queued` cron picks it up as
  soon as infrastructure recovers.
"""

from __future__ import annotations

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
    def __init__(self, gateway: TelegramGateway | None = None) -> None:
        # `gateway` is only used by `run_request` and is always provided by
        # the worker (the single MTProto owner). The bot uses this service
        # purely for submit/status/retrieve, which need no MTProto.
        self._gateway = gateway
        self._settings = get_settings()

    async def submit(
        self,
        text: str,
        user_id: int,
        *,
        launch_inline: bool = False,
    ) -> SubmitResult:
        if launch_inline:
            raise ValueError(
                "inline execution was removed: the bot never opens an MTProto session; "
                "requests are processed by the worker."
            )
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
        # Redis is down: the request is persisted and the worker's
        # `recover_queued` cron will enqueue it once Redis is back.
        return SubmitResult(request_id=request_id, queued=False, degraded=True)

    async def recover_queued(self, limit: int = 100) -> int:
        """Re-enqueue analysis requests left QUEUED by a Redis outage/crash.

        Called by the worker cron; only the worker process runs it, so it can
        never execute analysis inside the bot.
        """
        async with session_scope() as session:
            result = await session.execute(
                select(AnalysisRequest.id)
                .where(AnalysisRequest.status == AnalysisStatus.QUEUED.value)
                .order_by(AnalysisRequest.created_at)
                .limit(limit)
            )
            request_ids = [row[0] for row in result.all()]
        recovered = 0
        for request_id in request_ids:
            try:
                if await enqueue("analyze_message", request_id, job_id=request_id):
                    recovered += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("recover: enqueue failed request=%s err=%s", request_id, type(exc).__name__)
        if recovered:
            logger.info("analysis: recovered %d queued requests", recovered)
        return recovered

    async def run_request(self, request_id: str) -> AnalysisOutcome | None:
        """Execute one analysis job (worker entry point). Idempotent."""
        if self._gateway is None:
            raise RuntimeError("run_request requires an MTProto gateway (worker process only)")
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
            outcome = await pipeline.analyze(original, user_id=user_id)
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
        launch_inline: bool = False,
    ) -> SubmitResult:
        """Re-analyze the same request id with edited text."""
        if launch_inline:
            raise ValueError(
                "inline execution was removed: the bot never opens an MTProto session; "
                "requests are processed by the worker."
            )
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
        return SubmitResult(request_id=request_id, queued=False, degraded=True)
