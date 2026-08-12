#!/bin/sh
set -e

echo "[entrypoint] running database migrations"
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
