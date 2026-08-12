"""redis.asyncio client factory.

redis-py's `from_url()` does not derive `ssl=True` from the `rediss://`
scheme (it only picks up `ssl_cert_reqs`); without it a `rediss://` URL
silently connects WITHOUT TLS. All application call sites must use
`redis_from_url()` so that managed-platform TLS Redis (e.g. Northflank)
actually encrypts traffic.
"""

from __future__ import annotations

from urllib.parse import urlparse

from redis.asyncio import from_url


def redis_from_url(url: str, **kwargs):
    parsed = urlparse(url)
    if parsed.scheme == "rediss":
        kwargs.setdefault("ssl", True)
    return from_url(url, **kwargs)