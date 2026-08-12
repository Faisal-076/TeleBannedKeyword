FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN addgroup --system app && adduser --system --ingroup app app

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
