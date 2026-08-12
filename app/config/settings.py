"""Application configuration loaded from environment variables.

Secrets are never logged or exposed through any API. They are only read
into `SecretStr`-typed fields here and passed by reference to the components
that need them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class RiskWeights(BaseModel):
    """Configurable contribution of each evidence type to the risk score."""

    explicit_rule: float = 30.0
    regex: float = 28.0
    fuzzy: float = 22.0
    ai: float = 15.0
    unseen: float = 8.0
    rare: float = 4.0
    frequent_use: float = -14.0
    explicit_floor: float = 70.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Core ----
    environment: str = "development"
    log_level: str = "INFO"
    log_privacy_level: Literal["full", "medium", "minimal"] = "medium"

    # ---- Telegram Bot API ----
    bot_token: SecretStr = Field(default="")
    admin_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # ---- Admin HTTP API ----
    admin_api_key: SecretStr = Field(default="")

    # ---- Telegram MTProto ----
    telegram_api_id: int = 0
    telegram_api_hash: SecretStr = Field(default="")
    master_secret: SecretStr | None = None
    session_enc: str | None = None
    session_file: str | None = None

    # ---- Database / Redis ----
    database_url: str = "sqlite+aiosqlite:///./data/tbk.db"
    redis_url: str = "redis://localhost:6379/0"

    # ---- Analysis ----
    max_message_chars: int = 4000
    fuzzy_threshold: float = 0.88
    history_search_limit: int = 50
    data_retention_days: int = 90
    require_coverage_for_unseen: bool = True
    risk_weights: RiskWeights = RiskWeights()

    # ---- LLM ----
    llm_provider: str = "disabled"
    llm_api_key: SecretStr = Field(default="")
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 30.0
    llm_max_context_chars: int = 4000

    # ---- Indexing ----
    initial_sync_batch: int = 500
    initial_sync_max_messages: int = 200_000
    incremental_sync_batch: int = 500

    # ---- MTProto rate limiting ----
    mt_proto_max_concurrency: int = 3
    mt_proto_chat_min_interval: float = 0.6
    mt_proto_flood_sleep_threshold: int = 60
    mt_proto_max_flood_sleep: int = 3600
    mt_proto_retry_limit: int = 5

    # ---- API server ----
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ---- Worker ----
    worker_heartbeat_interval: int = 30
    worker_job_timeout: int = 3600

    @field_validator("risk_weights", mode="before")
    @classmethod
    def _parse_risk_weights(cls, value):
        if isinstance(value, dict):
            return value
        return value

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _parse_admin_user_ids(cls, value):
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @property
    def bot_configured(self) -> bool:
        return bool(self.bot_token.get_secret_value())

    @property
    def mtproto_configured(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash.get_secret_value())

    @property
    def session_configured(self) -> bool:
        return bool(self.session_enc or self.session_file)

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider not in ("", "disabled")

    def describe_sensitive(self) -> dict:
        """Non-secret description used by /health and logs."""
        return {
            "environment": self.environment,
            "bot_configured": self.bot_configured,
            "mtproto_configured": self.mtproto_configured,
            "session_configured": self.session_configured,
            "llm_provider": self.llm_provider if self.llm_enabled else "disabled",
            "admin_users": len(self.admin_user_ids),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
