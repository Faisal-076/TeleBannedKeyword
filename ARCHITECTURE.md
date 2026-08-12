# Architecture

## Components

- `app/telegram/gateway.py` — Telegram client (MTProto/Telethon): connection lifecycle,
  message search, paginated history iteration, chat resolution. **Worker-owned only.**
- `app/telegram/session_store.py` — encrypted Telethon session handling (provisioning,
  load, revoke, file wipe). **Worker-owned only.**
- `app/history/indexer.py` — incremental backfill of chat history into the local index:
  message text, hashes, extracted terms, per-term aggregates with context samples.
- `app/history/searcher.py` — evidence gathering: is a term seen/unseen/unknown in a chat's
  history (local index first, targeted Telegram search second, fuzzy frequency match third).
- `app/history/coverage.py` — history-coverage model: complete / partial / unknown.
- `app/analysis/normalize.py` — text normalization: clean view, deobfuscated view (leet /
  confusables / repeated chars), compact view (separator-stripped) for spaced/hyphenated variants.
- `app/analysis/tokenize.py` — token / bigram / trigram extraction for the unseen scan.
- `app/analysis/fuzzy.py` — rapidfuzz-backed similarity and obfuscation-equivalence checks.
- `app/analysis/pipeline.py` — the multi-stage analysis pipeline (see below).
- `app/rules/engine.py` — deterministic rule engine (exact / phrase / regex, allowlist
  suppression, fuzzy variant detection), with a cached regex compiler.
- `app/rules/repository.py` — rule persistence (CRUD, bulk import/export).
- `app/llm/` — optional semantic review (OpenAI-compatible provider; disabled provider by default).
- `app/services/analysis_service.py` — job lifecycle: submit → queue → run → persist → outcome.
- `app/services/queue.py` — arq/Redis job queue. When Redis is down `enqueue` returns False
  and the request stays QUEUED in the database (no inline execution anywhere).
- `app/services/session_state.py` — bot-visible session status: Postgres-only read/write of
  the revocation flag (the worker reports connected/username state via Redis).
- `app/services/chat_service.py` — chat management (add/verify/enable/sync commands).
- `app/services/status_service.py` — health/readiness aggregation; reads worker-reported
  MTProto state and heartbeats; never exposes secrets.
- `app/bot/` — aiogram bot: commands, callbacks, edit handling, auth middleware. No MTProto,
  no session material; it queues work and reads DB/Redis state.
- `app/api/` — FastAPI admin API (Bearer `ADMIN_API_KEY`), embedded in the bot process or
  run standalone; readiness semantics are role-specific (`/ready`).
- `app/workers/` — arq worker: the single owner of the scanner session; runs analysis,
  sync, revocation and retention jobs.
- `app/database/` — async SQLAlchemy engine/session management and ORM models.
- `app/security/` — redaction, hashing, AES-256-GCM crypto (worker-only session blobs).
- `app/logging_conf.py` — structured JSON logging with privacy-aware redaction.

## Data flow (analysis)

```
message text
  -> normalize_document: clean / deobfuscated / compact views
  -> rule engine: exact, phrase (variant-tolerant), regex (timed), allowlist suppression
  -> fuzzy variant scan: deobfuscated tokens vs deobfuscated rule terms
  -> unseen scan: tokens/bigrams not in the common-word list
  -> history evidence: per-finding seen/unseen/unknown via index + Telegram search
  -> optional LLM semantic review (merge into findings)
  -> score findings (risk 0-100) -> per-chat result -> global outcome
```

## Process model

- One process per role: **bot** (aiogram polling + embedded API), **worker**
  (arq jobs, MTProto), and an optional standalone **api** (dev tool). All
  share the same database.
- The **worker is the single owner of the scanner session**: it is the only
  process that opens the MTProto gateway, decrypts the session or touches
  the session file/volume. The bot and API never construct a gateway or a
  SessionStore; they only queue jobs and read worker-reported state
  (Redis: connection state/heartbeats; Postgres: revocation flag).
- Work is submitted as arq jobs (`analyze_message`). When Redis is
  unavailable there is **no inline execution**: the request stays QUEUED in
  the database and the worker's `recover_queued` cron re-enqueues it once
  infrastructure recovers. Status transitions are race-safe
  (QUEUED -> RUNNING -> DONE/FAILED).
- Edits to the original message trigger `recheck` on the same request id.

## Database schema

- `analysis_requests`, `analysis_results` — analysis jobs and their outcomes.
- `telegram_chats`, `telegram_messages`, `message_terms`, `phrase_occurrences` — history index.
- `rules` — scoped rules (global / chat), allowlists, priorities.
- `audit_events` — hashed-user audit trail of administrative operations.
- `app_state`, `user_settings` — small key-value / per-user state.

## Job queue & workers

- arq worker (`app/workers/worker.py`) consumes `analyze_message`; on completion it
  notifies the Telegram user via `app/workers/functions.py`.
- Retries are bounded; failures are recorded on the request.
- Cron jobs: heartbeat (30 s), `recover_queued` (15 s), nightly retention.
- Redis is a transport, not a store of truth: queued-but-undelivered requests live in
  Postgres and are recovered by `recover_queued` after any Redis outage.

## Security boundaries

- Admin-only commands; bot middleware authorizes by `ADMIN_USER_IDS`.
- API access requires `ADMIN_API_KEY` (Bearer, constant-time compare).
- Secrets are role-scoped by construction: the bot/API processes never receive
  `TELEGRAM_API_ID`/`HASH`, `MASTER_SECRET`, `SESSION_ENC` or a session volume;
  `app/config/validate.py` enforces this per role at startup.
- `MASTER_SECRET` exists only in the worker (and the local provisioning CLI).
- Secrets are never logged (structured redaction) and never stored in plaintext;
  session files are encrypted at rest (worker volume only).

## Deployment topology

- Docker (`Dockerfile`, `docker-compose.yml`), Railway (`railway.toml`) or
  Northflank (`NORTHFLANK.md`).
- Services: bot, worker, optional standalone api; PostgreSQL; Redis.
- The worker-only volume at `/data` holds the encrypted session file.

## Failure mode matrix

| Failure | Behavior |
| --- | --- |
| Redis down | submissions stay QUEUED in Postgres; `recover_queued` re-enqueues when Redis returns (no inline execution) |
| Telegram search fails | evidence falls back to UNKNOWN / coverage-based |
| Regex catastrophic backtracking | hard match-time timeout (0.5s) |
| LLM unavailable | disabled provider: analysis proceeds without semantic review |
| Chat inaccessible | that chat marked ERROR/UNKNOWN; other chats still analyzed |
| Session revoked | worker refuses to connect (`connect()` returns False); bot reports REVOKED from Postgres flag |
