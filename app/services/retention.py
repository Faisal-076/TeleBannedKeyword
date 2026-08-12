"""Data retention: purge analysis data and indexed history after a cutoff.

Privacy default: keep the minimum. Retention applies to analysis requests,
results, indexed messages, message terms and phrase aggregates.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import (
    AnalysisRequest,
    IndexedMessage,
    MessageTerm,
    PhraseOccurrence,
)
from app.utils.timeutil import retention_cutoff

logger = logging.getLogger("app.services.retention")


async def purge_expired() -> dict[str, int]:
    days = get_settings().data_retention_days
    cutoff = retention_cutoff(days)
    counts: dict[str, int] = {}
    async with session_scope() as session:
        for model, label in (
            (AnalysisRequest, "analysis"),
            (IndexedMessage, "messages"),
            (MessageTerm, "terms"),
            (PhraseOccurrence, "phrases"),
        ):
            result = await session.execute(delete(model).where(model.created_at < cutoff))
            counts[label] = result.rowcount or 0
    logger.info("retention: purged %s (cutoff=%s)", counts, cutoff.date())
    return counts
