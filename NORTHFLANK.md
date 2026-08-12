# Northflank deployment guide

Step-by-step deployment of the Telegram Message Analyzer on
[Northflank](https://northflank.com). Same image, two services on one
project — plus Postgres and Redis from the Northflank add-on catalog.

> Railway users: see `README.md` §7 + `railway.toml`. Everything in this
> guide maps 1:1 (volume handling differs only in the uid note below).

---

## 1. Architecture recap

```
Telegram (Bot API) ──► bot service ──► Postgres ──► worker service ──► Telegram (MTProto)
                          │                 ▲            │
                          ▼                 │            ▼
                    FastAPI (:8000)     Redis/arq     scanner session
                   /health /ready      job queue      (volume /data)
```

| Service | Command | Owns | Exposes a port? |
|---|---|---|---|
| `bot` | `python -m app.main bot` | Bot API polling + admin API | Yes — HTTP `:8000` |
| `worker` | `python -m app.main worker` | Scanner session (MTProto), all jobs | No |

**Hard rule: only the worker touches the scanner account.** The bot just
queues work and reads status. Never give the bot `SESSION_ENC`/`SESSION_FILE`.

## 2. Prerequisites

- A Northflank account with a Project created.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from <https://my.telegram.org/apps>
  (scanner account — see README §3).
- `BOT_TOKEN` from @BotFather (README §4).
- A `MASTER_SECRET` (README §11):
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- This repository pushed to GitHub/GitLab and connected to Northflank.

## 3. Deployment steps

1. **Create the project** → *Add Service* → **PostgreSQL** (Northflank
   catalog). Note the generated credential strings (Public / Internal URL).
2. **Create a second service** → **Redis**. Prefer the internal endpoint
   (not exposed publicly). Keep TLS on.
3. Copy the **internal** connection strings — they are only reachable
   between your project's services:
   - `DATABASE_URL` → `postgresql+asyncpg://…` (replace the `postgresql://`
     prefix with `postgresql+asyncpg://`).
   - `REDIS_URL` → `rediss://…` (add `?ssl_cert_reqs=none` if certificates
     are not required; the app forces TLS whenever the scheme is `rediss`).
4. **Create service 1: the bot.** *Add Service* → the connected repository
   → **Docker** (no build command needed; the Dockerfile alone is the build).
5. Docker: leave everything default. The image runs as **non-root uid
   10001** and expects nothing baked in.
6. `Startup` command: `python -m app.main bot`.
7. `Port` → add port **8000** (HTTP) and enable it for **internal and
   external** access if you want the admin API reachable (recommended:
   external access = **ingress**, not public — see step 22).
8. `Healthcheck`: **HTTP**, path `/health`, port 8000. This is how
   Northflank restarts a wedged bot.
9. Set the bot's environment (all as **secrets** unless noted, see §5
   table for "both"):
   - `BOT_TOKEN`, `ADMIN_USER_IDS`, `ADMIN_API_KEY`
   - `DATABASE_URL`, `REDIS_URL` (internal strings)
   - `AUTO_CREATE_SCHEMA=false` — the schema is owned by Alembic, which the
     entrypoint runs on every container start. *(defaults to `true` for
     local dev; never leave that in production)*
   - `MIGRATE_ON_STARTUP=1` (default; keep) and `DB_MAX_WAIT_SECONDS=120`
     (default). The entrypoint waits for Postgres before migrating, so
     boot order does not matter.
   - `API_HOST=0.0.0.0`, `API_PORT=8000`.
10. Deploy the bot. Watch its logs: you should see
    `[entrypoint] database ready, applying migrations` then
    `bot: polling started` (or similar).
11. **Create service 2: the worker.** Same repository, same Docker build.
12. `Startup` command: `python -m app.main worker`.
13. No port is needed and the worker should get **no** port. For the
    healthcheck: **disable it** (the worker process is long-running and
    exposes no port; `watchdog`/restart-on-failure covers crashes). Worker
    readiness is observable through the bot's `/health` `worker` block:
    `heartbeat_age` reports how fresh the worker's Redis heartbeat is
    (updated every 30 s; stale > 60 s = not ready) and `mtproto_connected`
    reports the scanner connection.
14. **Volume** → *Add volume* → mount path **`/data`** (worker only).
    - Northflank volumes are owned by a fixed internal uid. The image user
      is pinned to **uid 10001 / gid 10001** (`useradd --uid 10001` in the
      Dockerfile), so the container can always read/write the volume
      regardless of platform defaults.
    - This volume holds the encrypted scanner session
      (`SESSION_FILE=/data/session.enc`). It is **the only state** besides
      Postgres.
15. Worker environment (secrets unless noted):
    - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `MASTER_SECRET`
    - `SESSION_FILE=/data/session.enc` (plain — this is just a path),
      overwriting the volume's directory
    - `DATABASE_URL`, `REDIS_URL` (same internal strings as the bot;
      Northflank makes these available as shared/duplicated secrets)
    - `AUTO_CREATE_SCHEMA=false`, `MIGRATE_ON_STARTUP=1`
16. Deploy the worker. Expected first-run log:
    ```
    [entrypoint] waiting for database …
    [entrypoint] database ready, applying migrations
    worker: starting (max_jobs=5)
    session: MASTER_SECRET is required to decrypt the session   ← until step 19
    ```
    The worker **starts without provable session availability** — this is
    the documented bootstrap state; it keeps the worker able to report
    "not connected" and to handle `/logout` revocations while you
    provision (step 19). Missing variables that actually prevent work
    (API id/hash, master secret with a session present) fail startup by
    design (`app/config/validate.py`).

## 4. Health checks & monitoring

**Readiness is role-specific** (`/ready`): the bot-role app gates on DB +
`BOT_TOKEN` configured + allowlist set — it never depends on MTProto, so a
fresh bot deploy is ready before any scanner session is provisioned. A
standalone `api`-role app (`python -m app.main api`) gates on DB +
`ADMIN_API_KEY`. The worker has no HTTP server — its readiness is published
through the bot's `/health` `worker` block (derived from the Redis
heartbeat `tbk:heartbeat:worker`, updated every 30 s) and the
worker-reported MTProto state.

| Service | Check | Notes |
|---|---|---|
| bot | HTTP `GET /health` on :8000 | **liveness only**: 200 + `status: "ok"` whenever the process responds; dependency state is separate fields (`database`, `redis`, `mtproto`, `worker`, `analysis`); no secrets |
| bot | HTTP `GET /ready` | readiness: DB ok **and** bot configured; 503 otherwise; never MTProto-dependent |
| api (standalone) | HTTP `GET /ready` | readiness: DB ok **and** `ADMIN_API_KEY` set; 503 otherwise |
| worker | none (portless) | readiness via bot `/health` `worker.ready` (heartbeat ≤ 60 s) + `worker.mtproto_connected`; restart policy `ON_FAILURE` |

The bot never owns session material: it reads the scanner's connection
state from Redis (published by the worker) and the revocation flag from
Postgres (`app/services/session_state.py`) — no `SESSION_ENC`/`SESSION_FILE`
env and no session volume are ever given to the bot service. Replicas of
the same service are fine: migrations are serialized by a **Postgres
advisory lock** in `migrations/env.py` (a second replica's `alembic upgrade
head` waits, then no-ops), and the DB schema is shared.

## 5. Environment variables for Northflank

Secrets (`*`) must be marked **secret** so they are never exposed in the
dashboard or logs.

| Variable | bot | worker | Note |
|---|---|---|---|
| `BOT_TOKEN`* | yes | – | @BotFather token |
| `ADMIN_USER_IDS` | yes | – | comma-separated user ids allowed to use the bot; empty = lockout (startup fails loudly) |
| `ADMIN_API_KEY`* | yes | – | bearer token for `/api/v1/admin/*` |
| `TELEGRAM_API_ID` | – | yes | my.telegram.org app id |
| `TELEGRAM_API_HASH`* | – | yes | my.telegram.org app hash |
| `MASTER_SECRET`* | – | yes | key for session encryption (required once session material is present) |
| `SESSION_ENC`* | – | either | encrypted blob from `tbk-auth` (alternative to the file) |
| `SESSION_FILE` | – | either | `/data/session.enc` on the worker volume |
| `DATABASE_URL`* | yes | yes | internal asyncpg URL, e.g. `postgresql+asyncpg://…?ssl=require` |
| `REDIS_URL`* | yes | yes | internal, e.g. `rediss://:…@redis.internal:6379/0?ssl_cert_reqs=none` |
| `AUTO_CREATE_SCHEMA` | yes | yes | `false` — migrations run in the entrypoint |
| `MIGRATE_ON_STARTUP` | – | – | `1` (default); `0` only if migrations are run elsewhere |
| `DB_MAX_WAIT_SECONDS` | – | – | `120` (default); entrypoint caps DB wait here |
| `API_HOST`/`API_PORT` | yes | – | `0.0.0.0` / `8000` |
| `LOG_LEVEL`, `LOG_PRIVACY_LEVEL` | opt | opt | `INFO` / `medium` by default |

Everything else in `.env.example` (`LLM_*`, `FUZZY_THRESHOLD`, `DATA_RETENTION_DAYS`,
`MT_PROTO_*`, …) is optional and applies to both services.

## 6. Provisioning the scanner session (first deploy)

Session credentials are **only ever entered on your machine** — never in
the Northflank dashboard:

1. Locally: `python scripts/auth_session.py --phone +15551234567 --output session.enc`
   (README §5; needs `MASTER_SECRET` + `TELEGRAM_API_ID`/`HASH` **locally**).
2. Upload `session.enc` into the worker volume (Northflank volume editor:
   *Upload file* → `session.enc`). The file is AES-256-GCM encrypted with
   `MASTER_SECRET`, so it is safe in and out of the volume.
3. The worker picks the file up on its next session load (no restart
   required for `SESSION_FILE` — `SessionStore` re-reads the path when
   nothing is cached). Check `/authstatus` in the bot → `connected: true`.
4. Verify `/sync all incremental` works on a test chat.

**Rotating the session:** see README §16/§17. `/logout` marks the session
revoked and **wipes `/data/session.enc`**; after re-provisioning into the
volume, `/authstatus` recovers without a worker restart (the revoked flag
is cleared by the new provisioning step).

## 7. Operational notes

- **Secrets in logs**: the app redacts and never logs token/key values.
  `/health`, `/ready` and the admin status endpoint never include secrets
  (`app/api/app.py`, `status_service.collect_status(include_secrets=False)`).
- **Redis outage**: submissions stay `queued` in Postgres; the worker's
  `recover_queued` cron (every 15 s) re-enqueues them when Redis returns.
  The bot never runs analysis inline and never opens MTProto.
- **Postgres outage at startup**: the entrypoint retries for up to
  `DB_MAX_WAIT_SECONDS`; platform restart policies handle the rest.
- **Upgrades**: redeploy both services from the new image. `alembic upgrade
  head` runs per container under the advisory lock; schema changes are
  applied once.
- **Backups**: `pg_dump` the Postgres add-on; the session file is already
  encrypted — keep `MASTER_SECRET` out of any backup.

## 8. Validation on startup (what fails fast)

`app/config/validate.py` is role-scoped: each service validates **only**
its own variables, so a misconfigured deploy fails at `Startup` instead of
hours later:

- bot: `BOT_TOKEN` + non-empty `ADMIN_USER_IDS`.
- worker: `TELEGRAM_API_ID`/`HASH`; `MASTER_SECRET` whenever session
  material is present. A missing session (bootstrap) is tolerated and
  logged — never a crash.
- api (standalone only): `ADMIN_API_KEY`.

Corresponding regressions are pinned in
`tests/unit/test_deployment.py` (role validation, rediss TLS, Postgres DSN,
docker context hygiene, worker=single MTProto owner).