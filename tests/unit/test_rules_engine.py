from sqlalchemy import select

from app.analysis.models import EvidenceType
from app.analysis.normalize import normalize_document
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import Rule, RuleKind, RuleScope
from app.rules.engine import fuzzy_rule_matches, match_rules
from app.rules.repository import create_rule


async def _rules(doc_text: str) -> list:
    doc = normalize_document(doc_text)
    async with session_scope() as session:
        result = await session.execute(select(Rule).where(Rule.enabled.is_(True)))
        rules = list(result.scalars().all())
    return doc, rules


async def test_exact_match(db):
    await create_rule(scope="global", kind="exact", pattern="scam", category="scam")
    doc, rules = await _rules("This is a scam deal")
    matches = match_rules(doc, rules)
    assert len(matches) == 1
    assert matches[0].evidence == EvidenceType.EXPLICIT_RULE
    assert matches[0].category == "scam"


async def test_exact_no_match(db):
    await create_rule(scope="global", kind="exact", pattern="scam", category="scam")
    doc, rules = await _rules("perfectly normal message")
    assert match_rules(doc, rules) == []


async def test_case_insensitive_by_default(db):
    await create_rule(scope="global", kind="exact", pattern="ScAm", category="scam")
    doc, rules = await _rules("contains SCAM here")
    assert len(match_rules(doc, rules)) == 1


async def test_case_sensitive_rule(db):
    await create_rule(scope="global", kind="exact", pattern="ScAm", case_sensitive=True)
    doc, rules = await _rules("contains SCAM here")
    assert match_rules(doc, rules) == []
    doc, rules = await _rules("contains ScAm here")
    assert len(match_rules(doc, rules)) == 1


async def test_phrase_spaced_variant(db):
    await create_rule(scope="global", kind="phrase", pattern="free crypto", category="scam")
    doc, rules = await _rules("get free crypto now")
    assert len(match_rules(doc, rules)) == 1
    doc, rules = await _rules("get f-r-e-e c-r-y-p-t-o now")
    assert len(match_rules(doc, rules)) == 1


async def test_regex_match(db):
    await create_rule(scope="global", kind="regex", pattern=r"\b\d{3}-\d{4}\b", category="phone")
    doc, rules = await _rules("call me at 555-1234")
    matches = match_rules(doc, rules)
    assert len(matches) == 1
    assert matches[0].evidence == EvidenceType.REGEX
    assert matches[0].matched_text == "555-1234"


async def test_malformed_regex_skipped(db):
    await create_rule(scope="global", kind="regex", pattern="([unclosed", category="bad")
    doc, rules = await _rules("anything")
    assert match_rules(doc, rules) == []


async def test_allowlist_suppresses_block(db):
    await create_rule(scope="global", kind="exact", pattern="bitcoin", category="crypto", is_allowlist=True)
    await create_rule(scope="global", kind="exact", pattern="bitcoin", category="blocked")
    doc, rules = await _rules("I accept bitcoin")
    assert match_rules(doc, rules) == []


async def test_chat_scoped_rule_only_for_that_chat(db):
    await create_rule(scope="chat", chat_id=-100111, kind="exact", pattern="membership", category="local")
    doc, rules = await _rules("membership deal")
    chat_rules = [r for r in rules if r.chat_id == -100111 or r.scope == "global"]
    assert match_rules(doc, chat_rules) != []


async def test_fuzzy_rule_match_detects_variant(db):
    await create_rule(scope="global", kind="exact", pattern="bitcoin", category="crypto")
    doc, rules = await _rules("send btc to this wallet")
    fuzzy = fuzzy_rule_matches(doc, rules)
    # "btc" is short; verify no fuzzy for short tokens
    assert fuzzy == []
    doc, rules = await _rules("send bitecoin to this wallet")
    fuzzy = fuzzy_rule_matches(doc, rules)
    assert len(fuzzy) == 1
    assert fuzzy[0].evidence == EvidenceType.FUZZY_RULE_MATCH


async def test_fuzzy_threshold_avoids_noise(db):
    await create_rule(scope="global", kind="exact", pattern="bitcoin", category="crypto")
    doc, rules = await _rules("send groceries home")
    assert fuzzy_rule_matches(doc, rules) == []
