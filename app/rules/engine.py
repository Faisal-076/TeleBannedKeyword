"""Deterministic rule engine.

Supported rule kinds:
- exact   : substring match on the normalized text
- phrase  : multi-word match, tolerant to separators/leetspeak/confusables
- regex   : compiled with the `regex` module and a hard timeout to prevent
            catastrophic backtracking from blocking the event loop

Allowlist rules (is_allowlist=True) suppress other findings for the same
normalized fragment. Malformed patterns are skipped and reported — they never
crash the pipeline.
"""

from __future__ import annotations

import logging
import time

import regex as re_engine

from app.analysis.fuzzy import (
    best_fuzzy_match,
    deobfuscate_token,
    obfuscation_equivalent,
)
from app.analysis.models import EvidenceType, RuleMatch
from app.analysis.normalize import NormalizedDocument
from app.config import get_settings
from app.database.models import Rule, RuleKind

logger = logging.getLogger("app.rules")

REGEX_TIMEOUT_SECONDS = 0.5

# Compiled-regex cache: (rule_id, pattern, case_sensitive) -> compiled object
# (or None for invalid patterns). Keying on the pattern means edits and
# id reuse (tests) never serve a stale compilation.
_compiled_cache: dict[tuple[int, str, bool], object | None] = {}
_cache_time: dict[tuple[int, str, bool], float] = {}
_CACHE_TTL = 300.0


def compile_rule_pattern(rule: Rule) -> object | None:
    """Compile a regex rule; None if invalid.

    Note: the `regex` module (2026+) removed the compile-time `timeout`
    kwarg — timeouts are enforced at match time in `_match_rule`.
    """
    key = (rule.id, rule.pattern, rule.case_sensitive)
    cached = _compiled_cache.get(key)
    cached_at = _cache_time.get(key, 0.0)
    if cached is not None or (key in _compiled_cache and time.time() - cached_at < _CACHE_TTL):
        if time.time() - cached_at > _CACHE_TTL and key in _compiled_cache:
            cached = None
        return cached
    try:
        flags = 0
        if not rule.case_sensitive:
            flags |= re_engine.IGNORECASE
        compiled = re_engine.compile(rule.pattern, flags=flags)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule engine: invalid regex rule id=%s pattern=%r", rule.id, rule.pattern, extra={"extra": {"rule_id": rule.id}})
        _compiled_cache[key] = None
        _cache_time[key] = time.time()
        return None
    _compiled_cache[key] = compiled
    _cache_time[key] = time.time()
    return compiled


def invalid_regex_rules(rules: list[Rule]) -> list[Rule]:
    return [r for r in rules if r.kind == RuleKind.REGEX.value and compile_rule_pattern(r) is None]


def _match_rule(rule: Rule, doc: NormalizedDocument) -> str | None:
    """Return the matched text fragment, or None."""
    if rule.kind == RuleKind.REGEX.value:
        compiled = compile_rule_pattern(rule)
        if compiled is None:
            return None
        try:
            match = compiled.search(
                doc.original if rule.case_sensitive else doc.clean,
                timeout=REGEX_TIMEOUT_SECONDS,
            )
            return match.group(0) if match else None
        except TimeoutError:
            logger.warning("rule engine: regex timeout rule_id=%s", rule.id)
            return None
    if rule.kind == RuleKind.PHRASE.value:
        pattern = rule.pattern if rule.case_sensitive else rule.pattern.casefold()
        target = doc.original if rule.case_sensitive else doc.clean
        if pattern in target:
            return pattern
        # variant-tolerant phrase check
        if not rule.case_sensitive:
            from app.analysis.fuzzy import phrase_variant_equivalent

            if phrase_variant_equivalent(rule.pattern, doc.compact):
                return rule.pattern
        return None
    # exact
    pattern = rule.pattern if rule.case_sensitive else rule.pattern.casefold()
    target = doc.original if rule.case_sensitive else doc.clean
    if pattern in target:
        return pattern
    if not rule.case_sensitive:
        rule_token = deobfuscate_token(rule.pattern)
        if rule_token and rule_token in doc.deobfuscated.split(" "):
            return rule.pattern
    return None


def _evidence_for(rule: Rule) -> EvidenceType:
    if rule.kind == RuleKind.REGEX.value:
        return EvidenceType.REGEX
    return EvidenceType.EXPLICIT_RULE


def match_rules(doc: NormalizedDocument, rules: list[Rule]) -> list[RuleMatch]:
    """Deterministic matching with allowlist suppression.

    Allowlist rules never produce findings — they only suppress block-rule
    matches for the same normalized fragment.
    """
    findings: list[RuleMatch] = []
    allowlist_matches: set[str] = set()

    enabled = [r for r in rules if r.enabled]
    allow_rules = [r for r in enabled if r.is_allowlist]
    block_rules = [r for r in enabled if not r.is_allowlist]

    for rule in allow_rules:
        matched = _match_rule(rule, doc)
        if matched:
            allowlist_matches.add(matched.casefold())

    for rule in block_rules:
        matched = _match_rule(rule, doc)
        if not matched:
            continue
        if matched.casefold() in allowlist_matches:
            continue
        findings.append(
            RuleMatch(
                rule_id=rule.id,
                rule_kind=rule.kind,
                matched_text=matched,
                category=rule.category,
                evidence=_evidence_for(rule),
                scope=rule.scope,
                weight=float(rule.weight or 0.0),
                note=rule.note,
            )
        )
    return findings


def fuzzy_rule_matches(doc: NormalizedDocument, rules: list[Rule]) -> list[RuleMatch]:
    """Detect obfuscated/spelling-variant versions of block rules.

    Compares each message token (deobfuscated) against each single-token
    block rule (deobfuscated) with a configurable similarity threshold.
    Only words of length >= 4 are considered to limit false positives.
    """
    settings = get_settings()
    threshold = settings.fuzzy_threshold
    out: list[RuleMatch] = []
    block_rules = [r for r in rules if r.enabled and not r.is_allowlist]
    token_forms = {deobfuscate_token(t): t for t in doc.tokens if len(t) >= 4}
    for rule in block_rules:
        if rule.kind == RuleKind.REGEX.value:
            continue
        rule_token = deobfuscate_token(rule.pattern)
        if not rule_token or len(rule_token) < 4:
            continue
        candidates = [t for t in token_forms if t != rule_token]
        best, score = best_fuzzy_match(rule_token, candidates, threshold)
        if best is None:
            continue
        if obfuscation_equivalent(rule.pattern, token_forms[best]):
            continue
        out.append(
            RuleMatch(
                rule_id=rule.id,
                rule_kind=rule.kind,
                matched_text=token_forms[best],
                category=rule.category,
                evidence=EvidenceType.FUZZY_RULE_MATCH,
                scope=rule.scope,
                weight=float(rule.weight or 0.0) * 0.6,
                note=f"similar wording (score {score:.2f}) vs rule: {rule.pattern!r}",
            )
        )
    return out
