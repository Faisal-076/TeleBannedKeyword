# Security model

This project defends message content, user identifiers, and administrative secrets.
Not a production-hardened system without review: deployment environments differ.

## Secret inventory

| Secret | Where it lives | Protection |
| --- | --- | --- |
| `BOT_TOKEN` | env | server-side only; never logged |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | env | server-side only; never logged |
| `ADMIN_API_KEY` | env | required for every admin API call; constant-time compare |
| `MASTER_SECRET` | env | required for cryptographic admin operations |
| `SESSION_ENC` / session files | filesystem + env | encrypted at rest; never logged; never committed |
| `DATABASE_URL` | env | production deployments keep the database private (see `"database"` service in `docker-compose.yml`) |

## Secrets in logs

- Structured logging redacts bot tokens, API hashes, master secrets, session blobs,
  and long hex-like values (see `app/security/redact.py`, `app/logging_conf.py`).
- Message content is only logged at privacy level `full`; user ids are hashed at
  `medium`/`minimal`.
- No secret is ever written into `AuditEvent.details`; user references are hashed.

## Authentication & authorization

- Telegram space: commands are restricted to `ADMIN_USER_IDS` via middleware.
- API space: every request requires `X-Admin-Key`; the header is compared with a
  constant-time comparison; no credentials are logged.
- Cryptographic API routes additionally require `X-Master-Secret` (e.g., provisioning
  a session or rotating encryption keys).
- Rate limiting: the admin API is only bound to localhost in the default compose
  (behind the bot container) — keep it that way in production unless you add your own
  auth layer (e.g., VPN / mTLS).

## Data minimization

- Only the local index stores message text, trimmed to `INDEXED_TEXT_MAX` chars,
  with a hash for change detection; original Telegram messages are never re-downloaded
  after indexing.
- Media is never downloaded.
- Rules are stored as patterns; full rule exports require master-secret authority.

## Failure handling

- Access-restricted chats: `TelegramAccessError` carries a code and is never logged
  with the raw chat content.
- Degraded mode (Redis down) still runs analysis in-process; no security check is
  skipped in degraded mode.
- Invalid regex and regex timeouts never crash the pipeline.

## Operational guidance

- Rotate `BOT_TOKEN` and `ADMIN_API_KEY` on suspicion of exposure.
- `MASTER_SECRET` rotation requires re-encrypting stored session files — automate
  it or do it during a maintenance window.
- Keep `LOG_PRIVACY_LEVEL` at `medium` or `minimal` in production.
- The `/api/health` and `/api/ready` endpoints expose only non-sensitive connectivity
  status (no secrets, no message content).