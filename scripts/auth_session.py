"""Local MTProto authentication (run on YOUR machine, never on the server).

Authenticates the scanner account with phone + login code + optional 2FA,
produces an AES-256-GCM encrypted session, and prints it (SESSION_ENC) or
writes it to a file (SESSION_FILE) for the server.

Credentials are entered only in this local process. The raw session string
is NEVER printed; only the encrypted blob is emitted.

Usage:
    python scripts/auth_session.py --phone +15551234567
    python scripts/auth_session.py --phone +15551234567 --output session.enc
    python scripts/auth_session.py --qr

MASTER_SECRET (env var) must match the value configured on the server.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import tempfile

from app.config import get_settings
from app.security.crypto import CryptoError, encrypt_string
from app.security.redact import mask_phone, mask_username

logger = logging.getLogger("scripts.auth_session")

_QR_WAIT_SECONDS = 120


def _get_master_secret(args) -> str:
    master = args.master_secret or os.environ.get("MASTER_SECRET") or ""
    if not master:
        master = getpass.getpass("MASTER_SECRET (must match server): ").strip()
    if not master:
        raise SystemExit("MASTER_SECRET is required (env, --master-secret, or prompt).")
    return master


async def _login_qr(client) -> None:
    qr_login = await client.qr_login()
    print("Scan the QR code with the scanner account.")
    try:
        await qr_login.wait(timeout=_QR_WAIT_SECONDS)
    except TimeoutError:
        raise SystemExit("QR login timed out.")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"QR login failed: {type(exc).__name__}")


async def _login_phone(client, phone: str | None) -> None:
    from telethon import errors

    if not phone:
        phone = input("Phone number (international format, e.g. +15551234567): ").strip()
    await client.send_code_request(phone)
    code = getpass.getpass(f"Login code sent to {mask_phone(phone)}: ")
    try:
        await client.sign_in(phone=phone, code=code)
    except errors.SessionPasswordNeededError:
        password = getpass.getpass("2FA password: ")
        await client.sign_in(password=password)
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
        raise SystemExit("Invalid login code.")
    except errors.PhoneCodeEmptyError:
        raise SystemExit("Login code is required.")
    except errors.PhoneNumberUnoccupiedError:
        raise SystemExit("This phone has no Telegram account; registration is not supported.")


async def _authenticate(client, phone: str | None, use_qr: bool) -> None:
    if use_qr:
        await _login_qr(client)
    else:
        await _login_phone(client, phone)


async def _run(args) -> int:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.sessions import StringSession

    master = _get_master_secret(args)

    api_id = os.environ.get("TELEGRAM_API_ID") or getattr(get_settings(), "telegram_api_id", 0)
    api_hash = os.environ.get("TELEGRAM_API_HASH") or ""
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH are required "
                         "(https://my.telegram.org/apps).")
    api_id = int(api_id)

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await _authenticate(client, args.phone, args.qr)
        me = await client.get_me()
        if me is None:
            raise SystemExit("Authentication produced no account.")
        session_string = client.session.save()
        encrypted = encrypt_string(session_string, master)
        del session_string

        username = getattr(me, "username", None) or f"id{me.id}"
        phone = mask_phone(getattr(me, "phone", "") or "")
        print(f"Authenticated as @{mask_username(username)} (phone {phone})")
        if args.output:
            path = args.output
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(encrypted)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
            print(f"Encrypted session written to {path}")
            print("Set SESSION_FILE to this path and MASTER_SECRET on the server.")
        else:
            print("Encrypted session (set as SESSION_ENC on the server, with MASTER_SECRET):")
            print("SESSION_ENC=" + encrypted)
    except CryptoError as exc:
        print(f"Encryption failed: {exc}")
        return 1
    except FloodWaitError as exc:
        seconds = int(getattr(exc, "seconds", 60) or 60)
        print(f"Telegram rate limit: wait {seconds}s and retry.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Authentication failed: {type(exc).__name__}")
        return 1
    finally:
        await client.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Telegram MTProto session provisioning")
    parser.add_argument("--phone", help="scanner account phone (or prompted)")
    parser.add_argument("--qr", action="store_true", help="authenticate by scanning a QR code")
    parser.add_argument("--master-secret", help="encryption master secret (or MASTER_SECRET env)")
    parser.add_argument("--output", help="write encrypted session to this file (default: stdout)")
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except SystemExit as exc:
        print(str(exc) or "aborted.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
