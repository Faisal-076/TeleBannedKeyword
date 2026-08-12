"""FastAPI application: /health, /ready, authenticated admin endpoints."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

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
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.services.queue import enqueue
from app.services.status_service import collect_status

logger = logging.getLogger("app.api")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


def create_app(
    analysis: AnalysisService | None = None,
    chat_service: ChatService | None = None,
    *,
    role: Literal["bot", "api"] = "bot",
) -> FastAPI:
    """Build the admin API application.

    `role` tunes readiness semantics to the process that serves it:
    - "bot": the app is embedded in the bot process → /ready gates on the
      database AND the bot being configured (polling can start).
    - "api": the app runs standalone (dev tool) → /ready gates on the
      database AND the admin API being configured.
    /health is role-tagged and reports infra/worker state; it never blocks
    on other services (liveness, not readiness).
    """
    app = FastAPI(
        title="Telegram Message Analyzer API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    @app.get("/health")
    async def health() -> dict:
        status_data = await collect_status(include_secrets=False)
        return {
            "status": "ok" if status_data["database"] == "ok" else "degraded",
            "role": role,
            "database": status_data["database"],
            "redis": status_data["redis"],
            "mtproto": status_data["mtproto"],
            "bot_api": status_data["bot_api"],
            "worker_heartbeat_age": status_data["worker_heartbeat_age"],
            "analysis": status_data["analysis"],
        }

    @app.get("/ready")
    async def ready() -> dict:
        status_data = await collect_status()
        db_ok = status_data["database"] == "ok"
        if role == "bot":
            ready_ok = db_ok and status_data["bot_api"]["configured"]
        else:
            settings = get_settings()
            ready_ok = db_ok and bool(settings.admin_api_key.get_secret_value())
        if not ready_ok:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"database": status_data["database"], "role": role},
            )
        return {"ready": True, "role": role}

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
    async def admin_add_chat(payload: ChatAddRequest) -> ChatAddResponse:
        # Chat resolution requires MTProto; it runs ONLY in the worker.
        # The API queues the job and never opens an MTProto session.
        queued = await enqueue(
            "add_chat", payload.reference, "api",
            job_id=f"add-chat:{payload.reference}",
        )
        if not queued:
            return ChatAddResponse(ok=False, error="worker unavailable (redis down)")
        return ChatAddResponse(ok=True, queued=True)

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
        return await collect_status(include_secrets=False)

    app.include_router(admin)
    return app


def _chats_dep(chat_service: ChatService | None):
    async def dependency() -> ChatService:
        if chat_service is not None:
            return chat_service
        # DB-only service: no MTProto gateway is ever created in the API
        # process. Chat resolution is queued to the worker.
        return ChatService()

    return Depends(dependency)
