"""Admin API authentication (Bearer token from ADMIN_API_KEY)."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status

from app.config import get_settings


def _check_api_key(authorization: str | None) -> None:
    settings = get_settings()
    expected = settings.admin_api_key.get_secret_value()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin api not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid token")


async def require_admin(
    authorization: str | None = Header(default=None),
) -> None:
    _check_api_key(authorization)


ADMIN_DEPENDENCY = Depends(require_admin)
