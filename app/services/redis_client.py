"""redis.asyncio client factory.

redis-py >=5 `from_url()` maps the `rediss://` scheme to its TLS-capable
`SSLConnection` class automatically (no `ssl=True` kwarg needed — that
keyword is not accepted by the asyncio `AbstractConnection`). All
application call sites must use `redis_from_url()` so that managed-platform
TLS Redis (e.g. Northflank/Upstash) actually encrypts traffic; as an extra
guard we enable certificate hostname verification on `rediss://`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from redis.asyncio import from_url

from app.config import get_settings

# Health-path Redis bound: /health is the k8s liveness probe target with a
# 5s timeout; a stalled rediss:// TLS handshake with an unbounded client
# would hang /health past the probe and get the pod killed. Keep every
# status reader/writer bounded well below the probe timeout.
HEALTH_REDIS_TIMEOUT = 1.5


def redis_from_url(url: str, **kwargs):
    parsed = urlparse(url)
    if parsed.scheme == "rediss":
        kwargs.setdefault("ssl_check_hostname", True)
    return from_url(url, **kwargs)


def redis_health_from_url(**kwargs):
    """Bounded redis client for /health and heartbeat paths.

    FAIL-FAST contract: connect and each command must give up in
    `HEALTH_REDIS_TIMEOUT` seconds so the liveness probe can never stall.
    Callers must treat timeouts/errors as "unavailable" and degrade.
    """
    settings = get_settings()
    kwargs.setdefault("socket_connect_timeout", HEALTH_REDIS_TIMEOUT)
    kwargs.setdefault("socket_timeout", HEALTH_REDIS_TIMEOUT)
    return redis_from_url(settings.redis_url, decode_responses=True, **kwargs)