"""Database bootstrap for local development.

Production deployments run `alembic upgrade head` (see Dockerfile/README).
"""

from __future__ import annotations

import logging

from app.database.engine import get_engine
from app.database.models import Base

logger = logging.getLogger("app.database.init")


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database: schema ensured (dev mode)")
