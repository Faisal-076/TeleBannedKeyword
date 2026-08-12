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


def redis_from_url(url: str, **kwargs):
    parsed = urlparse(url)
    if parsed.scheme == "rediss":
        kwargs.setdefault("ssl_check_hostname", True)
    return from_url(url, **kwargs)