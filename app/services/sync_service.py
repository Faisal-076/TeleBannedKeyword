"""History synchronization orchestration (job entry + direct invocation)."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.database.engine import session_scope
from app.database.models import SyncState, TargetChat
from app.history.indexer import HistoryIndexer, SyncReport
from app.telegram.gateway import TelegramGateway

logger = logging.getLogger("app.services.sync")


class SyncService:
    def __init__(self, gateway: TelegramGateway, indexer: HistoryIndexer | None = None) -> None:
        self._gateway = gateway
        self._indexer = indexer or HistoryIndexer(gateway)

    async def sync_chat(self, chat_id: int, mode: str = "incremental") -> SyncReport | None:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).where(TargetChat.telegram_chat_id == chat_id)
            )
            chat = result.scalar_one_or_none()
            if chat is None:
                return None
            chat.sync_state = SyncState.PENDING.value
        try:
            return await self._indexer.sync_chat(chat, mode)
        finally:
            logger.info("sync: chat=%s mode=%s finished", chat_id, mode)

    async def sync_all(self, mode: str = "incremental") -> list[SyncReport]:
        async with session_scope() as session:
            result = await session.execute(select(TargetChat).where(TargetChat.enabled.is_(True)))
            chats = list(result.scalars().all())
        reports: list[SyncReport] = []
        for chat in chats:
            chat.sync_state = SyncState.PENDING.value
            report = await self._indexer.sync_chat(chat, mode)
            reports.append(report)
        return reports
