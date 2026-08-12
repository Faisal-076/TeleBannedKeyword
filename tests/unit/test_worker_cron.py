"""Regression tests for worker cron schedules.

arq 0.26 cron fields accept ``int`` or ``set/list/tuple`` values only; cron
expression strings such as ``"*/30"`` raise ``RuntimeError`` on the first
heartbeat (``cron.py: _get_next_dt``), crashing the worker right after startup.
"""

from __future__ import annotations

from datetime import UTC, datetime

from arq.cron import next_cron

from app.workers.worker import build_worker


def test_worker_cron_schedules_calculate_next_without_error() -> None:
    """Every registered cron job must compute its next run (this is what the
    worker does on the first heartbeat; it crashed here with "*/30")."""
    worker = build_worker()
    assert len(worker.cron_jobs) == 3
    now = datetime.now(UTC)
    for job in worker.cron_jobs:
        job.calculate_next(now)


def test_heartbeat_cron_runs_every_30_seconds() -> None:
    base = datetime(2026, 8, 12, 6, 0, 0, tzinfo=UTC)
    assert next_cron(base, second=set(range(0, 60, 30))) == datetime(
        2026, 8, 12, 6, 0, 30, 123456, tzinfo=UTC
    )


def test_recover_cron_runs_every_15_seconds() -> None:
    base = datetime(2026, 8, 12, 6, 0, 0, tzinfo=UTC)
    assert next_cron(base, second=set(range(0, 60, 15))) == datetime(
        2026, 8, 12, 6, 0, 15, 123456, tzinfo=UTC
    )
