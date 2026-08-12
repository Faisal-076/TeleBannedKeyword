"""Identifier generation for analysis requests and jobs."""

from __future__ import annotations

import secrets


def new_analysis_id() -> str:
    return secrets.token_urlsafe(12)


def new_job_id() -> str:
    return secrets.token_urlsafe(8)
