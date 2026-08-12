# Security model

This project defends message content, user identifiers, and administrative secrets.
Not a production-hardened system without review: deployment environments differ.

## Secret inventory

| Secret | Where it lives | Protection |
| --- | --- | --- |
| `BOT_TOKEN` | env (bot service) | server-side only; never logged |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | env (worker service) | worker-only; never logged |
| `ADMIN_API_KEY` | env (bot service) | required for every admin API call; constant-time compare |
| `MASTER_SECRET` | env (worker service) | worker-only; required to decrypt the session |
| `SESSION_ENC` / session files | worker volume + env | worker-only; encrypted at rest; never logged; never committed |
| `DATABASE_URL` | env | production deployments keep the database private (see `"database"` service in `docker-compose.yml`) |

**Role-scoped secrets.** The bot/API processes never receive MTProto or session
material (`TELEGRAM_API_ID/HASH`, `MASTER_SECRET`, `SESSION_ENC`, session volume);
`app/config/validate.py` enforces this per role at startup. The bot reads session
status only from worker-reported state: Redis for connection state, Postgres for
the revocation flag — it has no session store of its own.

## Secrets in logs

- Structured logging redacts bot tokens, API hashes, master secrets, session blobs,
  and long hex-like values (see `app/security/redact.py`, `app/logging_conf.py`).
- Message content is only logged at privacy level `full`; user ids are hashed at
  `medium`/`minimal`.
- No secret is ever written into `AuditEvent.details`; user references are hashed.

## Authentication & authorization

- Telegram space: commands are restricted to `ADMIN_USER_IDS` via middleware.
- API space: every request requires the `Authorization: Bearer <ADMIN_API_KEY>`
  header, compared with a constant-time comparison; no credentials are logged.
- Session provisioning is **CLI-only** (`scripts/auth_session.py`, run locally);
  there are no cryptographic API routes. The bot never asks for phone / code /
  2FA / session strings in chat.
- The admin API is embedded in the bot process (or run standalone); bind it to
  a private network unless you add your own auth layer (e.g., VPN / mTLS).

## Data minimization

- Only the local index stores message text, trimmed to `INDEXED_TEXT_MAX` chars,
  with a hash for change detection; original Telegram messages are never re-downloaded
  after indexing.
- Media is never downloaded.
- Rules are stored as patterns; full rule exports require master-secret authority.

## Failure handling

- Access-restricted chats: `TelegramAccessError` carries a code and is never logged
  with the raw chat content.
- Redis down: no inline execution ever happens. Submissions stay QUEUED in Postgres
  and the worker's `recover_queued` cron re-enqueues them; no security check is
  skipped during the outage.
- Invalid regex and regex timeouts never crash the pipeline.
- Session revoked (via `/logout` fallback, Redis down): the bot sets the Postgres
  revocation flag only; the worker refuses to reconnect while it is set.

## Operational guidance

- Rotate `BOT_TOKEN` and `ADMIN_API_KEY` on suspicion of exposure.
- `MASTER_SECRET` rotation requires re-encrypting stored session files — automate
  it or do it during a maintenance window.
- Keep `LOG_PRIVACY_LEVEL` at `medium` or `minimal` in production.
- `/health` and `/ready` expose only non-sensitive connectivity status and
  role-tagged readiness (no secrets, no message content).