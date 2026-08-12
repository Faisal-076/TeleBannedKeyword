"""Database-backed rule storage (no code edits required to manage rules)."""

from __future__ import annotations

from sqlalchemy import delete, select, update

from app.database.engine import session_scope
from app.database.models import Rule, RuleKind, RuleScope


async def list_rules(chat_id: int | None = None) -> list[Rule]:
    stmt = select(Rule).order_by(Rule.priority, Rule.id)
    if chat_id is not None:
        stmt = stmt.where(Rule.chat_id == chat_id)
    async with session_scope() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_effective_rules(chat_id: int) -> list[Rule]:
    """Global rules plus rules scoped to the given chat."""
    stmt = select(Rule).where(
        Rule.enabled.is_(True),
        (Rule.scope == RuleScope.GLOBAL.value) | (Rule.chat_id == chat_id),
    ).order_by(Rule.priority, Rule.id)
    async with session_scope() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def create_rule(
    *,
    scope: str,
    kind: str,
    pattern: str,
    category: str = "general",
    chat_id: int | None = None,
    is_allowlist: bool = False,
    case_sensitive: bool = False,
    weight: float | None = None,
    note: str | None = None,
    enabled: bool = True,
    created_by: str | None = None,
) -> Rule:
    rule = Rule(
        scope=scope,
        kind=kind,
        pattern=pattern,
        category=category,
        chat_id=chat_id,
        is_allowlist=is_allowlist,
        case_sensitive=case_sensitive,
        weight=weight,
        note=note,
        enabled=enabled,
        created_by=created_by,
    )
    async with session_scope() as session:
        session.add(rule)
        await session.flush()
        await session.refresh(rule)
        return rule


async def update_rule(rule_id: int, **fields) -> Rule | None:
    allowed = {
        "pattern", "category", "note", "enabled", "weight", "case_sensitive",
        "is_allowlist", "priority",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None
    async with session_scope() as session:
        await session.execute(
            update(Rule).where(Rule.id == rule_id).values(**updates)
        )
        result = await session.execute(select(Rule).where(Rule.id == rule_id))
        return result.scalar_one_or_none()


async def delete_rule(rule_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(delete(Rule).where(Rule.id == rule_id))
        return result.rowcount > 0


async def import_rules_bulk(rules: list[dict]) -> int:
    """Import rules from a YAML/JSON dump (scripts/import_rules.py)."""
    created = 0
    async with session_scope() as session:
        for item in rules:
            rule = Rule(
                scope=item.get("scope", RuleScope.GLOBAL.value),
                kind=item.get("kind", RuleKind.EXACT.value),
                pattern=item["pattern"],
                category=item.get("category", "general"),
                chat_id=item.get("chat_id"),
                is_allowlist=item.get("allow", False),
                case_sensitive=item.get("case_sensitive", False),
                weight=item.get("weight"),
                note=item.get("note"),
                enabled=item.get("enabled", True),
                priority=item.get("priority", 100),
                created_by=item.get("created_by", "import"),
            )
            session.add(rule)
            created += 1
    return created
