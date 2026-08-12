"""FastAPI application: /health, /ready, authenticated admin endpoints."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status

from app.api.schemas import (
    ChatAddRequest,
    ChatAddResponse,
    RuleCreateRequest,
    RuleResponse,
    SyncRequest,
)
from app.api.security import ADMIN_DEPENDENCY
from app.config import get_settings
from app.rules import repository as rules_repo
from app.services.chat_service import ChatService
from app.services.queue import enqueue
from app.services.status_service import collect_status
from app.telegram.gateway import TelegramGateway

logger = logging.getLogger("app.api")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


def create_app(
    gateway: TelegramGateway | None,
    analysis: AnalysisService | None = None,
    chat_service: ChatService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Telegram Message Analyzer API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    @app.get("/health")
    async def health() -> dict:
        status_data = await collect_status(gateway, include_secrets=False)
        return {
            "status": "ok" if status_data["database"] == "ok" else "degraded",
            "database": status_data["database"],
            "redis": status_data["redis"],
            "mtproto": status_data["mtproto"],
            "bot_api": status_data["bot_api"],
            "worker_heartbeat_age": status_data["worker_heartbeat_age"],
            "analysis": status_data["analysis"],
        }

    @app.get("/ready")
    async def ready() -> dict:
        status_data = await collect_status(gateway)
        db_ok = status_data["database"] == "ok"
        bot_ok = status_data["bot_api"]["configured"]
        if not (db_ok and bot_ok):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"database": status_data["database"], "bot_api": bot_ok},
            )
        return {"ready": True}

    admin = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[ADMIN_DEPENDENCY])

    @admin.get("/chats")
    async def admin_list_chats(chats: ChatService = _chats_dep(chat_service)) -> dict:
        chat_list = await chats.list_chats()
        return {
            "chats": [
                {
                    "id": c.id,
                    "telegram_chat_id": c.telegram_chat_id,
                    "title": c.title,
                    "username": c.username,
                    "chat_type": c.chat_type,
                    "access_state": c.access_state,
                    "enabled": c.enabled,
                    "sync_state": c.sync_state,
                    "sync_indexed_count": c.sync_indexed_count,
                    "sync_estimate": c.sync_estimate,
                    "topic_id": c.topic_id,
                }
                for c in chat_list
            ]
        }

    @admin.post("/chats")
    async def admin_add_chat(
        payload: ChatAddRequest,
        chats: ChatService = _chats_dep(chat_service),
    ) -> ChatAddResponse:
        result = await chats.add_chat(payload.reference, actor="api")
        if not result.ok or result.chat is None:
            return ChatAddResponse(ok=False, error=result.error)
        return ChatAddResponse(
            ok=True,
            chat_id=result.chat.telegram_chat_id,
            title=result.chat.title,
            username=result.chat.username,
            chat_type=result.chat.chat_type,
        )

    @admin.delete("/chats/{chat_id}")
    async def admin_delete_chat(chat_id: int, chats: ChatService = _chats_dep(chat_service)) -> dict:
        removed = await chats.remove_chat(chat_id)
        return {"removed": removed}

    @admin.post("/chats/{chat_id}/sync")
    async def admin_sync_chat(
        chat_id: int,
        payload: SyncRequest | None = None,
        chats: ChatService = _chats_dep(chat_service),
    ) -> dict:
        mode = (payload.mode if payload else "incremental") or "incremental"
        if mode not in ("initial", "incremental", "resync"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid mode")
        queued = await enqueue("sync_chat", chat_id, mode, job_id=f"sync:{chat_id}:{mode}")
        return {"queued": queued, "mode": mode}

    @admin.get("/rules")
    async def admin_list_rules() -> list[dict]:
        rules = await rules_repo.list_rules()
        return [
            {
                "id": r.id,
                "scope": r.scope,
                "kind": r.kind,
                "pattern": r.pattern,
                "category": r.category,
                "chat_id": r.chat_id,
                "allow": r.is_allowlist,
                "enabled": r.enabled,
                "weight": r.weight,
                "note": r.note,
            }
            for r in rules
        ]

    @admin.post("/rules")
    async def admin_create_rule(payload: RuleCreateRequest) -> RuleResponse:
        if payload.kind not in ("exact", "phrase", "regex"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be exact|phrase|regex")
        if payload.kind == "regex":
            import regex

            try:
                regex.compile(payload.pattern)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid regex: {exc}") from exc
        rule = await rules_repo.create_rule(
            scope=payload.scope,
            kind=payload.kind,
            pattern=payload.pattern,
            category=payload.category,
            chat_id=payload.chat_id,
            is_allowlist=payload.allow,
            case_sensitive=payload.case_sensitive,
            weight=payload.weight,
            note=payload.note,
            enabled=payload.enabled,
            created_by="api",
        )
        return RuleResponse(
            id=rule.id, scope=rule.scope, kind=rule.kind, pattern=rule.pattern,
            category=rule.category, chat_id=rule.chat_id, allow=rule.is_allowlist,
            enabled=rule.enabled, weight=rule.weight, note=rule.note,
        )

    @admin.delete("/rules/{rule_id}")
    async def admin_delete_rule(rule_id: int) -> dict:
        deleted = await rules_repo.delete_rule(rule_id)
        return {"deleted": deleted}

    @admin.get("/status")
    async def admin_status() -> dict:
        return await collect_status(gateway, include_secrets=False)

    app.include_router(admin)
    return app


def _chats_dep(chat_service: ChatService | None):
    async def dependency() -> ChatService:
        if chat_service is not None:
            return chat_service
        from app.telegram.gateway import create_gateway

        return ChatService(create_gateway())

    return Depends(dependency)
