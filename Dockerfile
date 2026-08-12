FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Pinned uid/gid (10001) so a mounted volume's file ownership is deterministic
# regardless of the container platform (see NORTHFLANK.md).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --upgrade pip && pip install .

RUN mkdir -p /data && chown app:app /data

COPY scripts/docker-entrypoint.sh /usr/local/bin/tbk-entrypoint
RUN chmod +x /usr/local/bin/tbk-entrypoint

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/tbk-entrypoint"]
CMD ["python", "-m", "app.main", "bot"]
