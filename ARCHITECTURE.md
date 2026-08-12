# Architecture

## Components

- `app/telegram/gateway.py` — Telegram API client (MTProto/Telethon-style interface):
  connection lifecycle, message search, paginated history iteration, chat resolution.
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
- `app/services/queue.py` — arq/Redis job queue with graceful degraded mode when Redis is down.
- `app/services/chat_service.py` — chat management (add/verify/enable/sync commands).
- `app/services/status_service.py` — health/readiness aggregation.
- `app/bot/` — aiogram bot: commands, callbacks, edit handling, auth middleware.
- `app/api/` — FastAPI admin API (auth via API key + master secret), behind the bot by default.
- `app/workers/` — arq worker entry point and job functions (run analysis, notify).
- `app/database/` — async SQLAlchemy engine/session management and ORM models.
- `app/security/` — redaction, hashing, crypto for session files, secret verification.
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

- One process per role (bot / worker / api / scheduler), all share the same database.
- Work is submitted as arq jobs (`analyze_message`); when Redis is unavailable the bot
  falls back to running jobs in-process (degraded mode). Status transitions are
  race-safe (QUEUED -> RUNNING -> DONE/FAILED).
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

## Security boundaries

- Admin-only commands; bot middleware authorizes by `ADMIN_USER_IDS`.
- API access requires `ADMIN_API_KEY` (constant-time check) plus `MASTER_SECRET` for
  cryptographic operations (session provisioning).
- Secrets are never logged (structured redaction) and never stored in plaintext;
  session files are encrypted at rest.

## Deployment topology

- Docker (see `Dockerfile`, `docker-compose.yml`) or Railway (`railway.toml`).
- Services: bot, worker, api (optional), scheduler (chat syncs), Redis, PostgreSQL.

## Failure mode matrix

| Failure | Behavior |
| --- | --- |
| Redis down | degraded mode: jobs run in-process; status API reports redis unavailable |
| Telegram search fails | evidence falls back to UNKNOWN / coverage-based |
| Regex catastrophic backtracking | hard match-time timeout (0.5s) |
| LLM unavailable | disabled provider: analysis proceeds without semantic review |
| Chat inaccessible | that chat marked ERROR/UNKNOWN; other chats still analyzed |
