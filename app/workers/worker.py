"""arq worker runner (production command: `tbk-worker`).

The worker process is the single owner of the MTProto scanner session.
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings
from arq.worker import Worker

from app.config import get_settings
from app.logging_conf import configure_logging
from app.workers.functions import (
    add_chat,
    analyze_message,
    check_chat,
    heartbeat,
    recover_queued,
    revoke_session,
    run_retention,
    shutdown,
    startup,
    sync_chat,
)


def build_worker() -> Worker:
    settings = get_settings()
    return Worker(
        functions=[
            analyze_message,
            sync_chat,
            add_chat,
            check_chat,
            revoke_session,
            run_retention,
            recover_queued,
            heartbeat,
        ],
        cron_jobs=[
            cron(heartbeat, second="*/30"),
            cron(recover_queued, second="*/15"),
            cron(run_retention, minute=0, hour=3, run_at_startup=False),
        ],
        redis_settings=RedisSettings.from_dsn(settings.redis_url),
        on_startup=startup,
        on_shutdown=shutdown,
        max_jobs=5,
        job_timeout=settings.worker_job_timeout,
        keep_result=3600,
        health_check_interval=15,
    )


async def run_worker() -> None:
    configure_logging(get_settings().log_level, get_settings().log_privacy_level)
    worker = build_worker()
    logger = logging.getLogger("app.workers.runner")
    logger.info("worker: starting (max_jobs=%d)", worker.max_jobs)
    await worker.async_run()


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_worker())
