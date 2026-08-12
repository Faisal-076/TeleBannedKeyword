#!/bin/sh
# Docker entrypoint: waits for the database, applies migrations, then runs CMD.
# - The retry loop tolerates the DB becoming reachable after the container starts.
# - MIGRATE_ON_STARTUP=0 disables migrations (use when another process owns them).
# - DB_MAX_WAIT_SECONDS caps how long we wait for the database before failing.
set -e

: "${MIGRATE_ON_STARTUP:=1}"
: "${DB_MAX_WAIT_SECONDS:=120}"

if [ "$MIGRATE_ON_STARTUP" = "1" ]; then
    echo "[entrypoint] waiting for database (max ${DB_MAX_WAIT_SECONDS}s)"
    waited=0
    until alembic current >/dev/null 2>&1; do
        waited=$((waited + 5))
        if [ "$waited" -ge "$DB_MAX_WAIT_SECONDS" ]; then
            echo "[entrypoint] database unreachable after ${waited}s; giving up" >&2
            exit 1
        fi
        echo "[entrypoint] database not ready (${waited}s elapsed); retrying in 5s"
        sleep 5
    done
    echo "[entrypoint] database ready, applying migrations"
    alembic upgrade head
fi

echo "[entrypoint] starting: $*"
exec "$@"
