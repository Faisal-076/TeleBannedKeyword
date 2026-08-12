"""Encrypt/decrypt arbitrary values with the MASTER_SECRET.

Used for secret rotation and provisioning. Never prints secrets to logs.

Usage:
    python scripts/encrypt_value.py encrypt "value"            # prints v1:...
    python scripts/encrypt_value.py decrypt "v1:..."           # prints plaintext
    python scripts/encrypt_value.py --master-secret X encrypt "value"
"""

from __future__ import annotations

import argparse
import sys

from app.security.crypto import decrypt_string, encrypt_string


def main() -> int:
    parser = argparse.ArgumentParser(description="AES-256-GCM encrypt/decrypt via MASTER_SECRET")
    parser.add_argument("action", choices=["encrypt", "decrypt"])
    parser.add_argument("value", help="plaintext (encrypt) or blob (decrypt)")
    parser.add_argument("--master-secret", default=None)
    args = parser.parse_args()

    import os

    master = args.master_secret or os.environ.get("MASTER_SECRET", "")
    if not master:
        print("MASTER_SECRET is required", file=sys.stderr)
        return 1
    try:
        if args.action == "encrypt":
            print(encrypt_string(args.value, master))
        else:
            print(decrypt_string(args.value, master))
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
