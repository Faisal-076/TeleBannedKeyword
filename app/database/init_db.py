"""Database bootstrap.

Local development creates the schema on startup. Production deployments
run `alembic upgrade head` (see Dockerfile/NORTHFLANK.md) and set
`AUTO_CREATE_SCHEMA=false` so the schema is exclusively managed by Alembic.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.database.engine import get_engine
from app.database.models import Base

logger = logging.getLogger("app.database.init")


async def init_db() -> None:
    settings = get_settings()
    if not settings.auto_create_schema:
        logger.info(
            "database: AUTO_CREATE_SCHEMA=false, skipping schema creation "
            "(use alembic upgrade head)"
        )
        return
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database: schema ensured (dev mode)")
