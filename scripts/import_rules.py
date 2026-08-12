"""Import rules from a YAML/JSON file into the database.

Example rules.yaml:
    rules:
      - kind: exact
        pattern: "free crypto"
        category: scam
        note: "common scam phrase"
      - kind: regex
        pattern: "\\b(promocode|promo code)\\b"
        category: promo
      - kind: phrase
        pattern: "click this link"
        category: phishing
        allow: true

Usage:
    python scripts/import_rules.py rules.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.rules.repository import import_rules_bulk


def _load(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            raise SystemExit("PyYAML is required for YAML import: pip install pyyaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise SystemExit("Expected a list of rules (or {\"rules\": [...]}).")
    for item in data:
        if "pattern" not in item:
            raise SystemExit("Each rule requires a 'pattern'.")
    return data


async def _run(path: Path) -> int:
    rules = _load(path)
    created = await import_rules_bulk(rules)
    print(f"Imported {created} rules from {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import moderation rules")
    parser.add_argument("file", help="rules.yaml / rules.json")
    args = parser.parse_args()
    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    return asyncio.run(_run(path))


if __name__ == "__main__":
    sys.exit(main())
