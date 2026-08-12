"""Fuzzy string matching over normalized/deobfuscated terms.

Uses rapidfuzz (C++-backed) for stable, fast ratio scoring. All comparisons
operate on deobfuscated forms so obfuscation variants (`w0rd`, `w-o-r-d`,
`w o r d`, repeated chars) collapse to the same candidate before scoring.
"""

from __future__ import annotations

from rapidfuzz import fuzz, process

from app.analysis.normalize import compact_text, deobfuscate_token


def similarity(a: str, b: str) -> float:
    """Normalized edit-similarity in [0, 1] (0=identical)."""
    return fuzz.ratio(a, b) / 100.0


def best_fuzzy_match(
    term: str, candidates: list[str], threshold: float
) -> tuple[str | None, float]:
    """Return (best_candidate, score) or (None, 0.0) if below threshold.

    `term` and candidates should be deobfuscated tokens.
    """
    if not term or not candidates:
        return None, 0.0
    best, score, *_ = process.extractOne(term, candidates, scorer=fuzz.ratio)
    sim = score / 100.0
    if sim >= threshold:
        return best, sim
    return None, 0.0


def obfuscation_equivalent(rule_pattern: str, token: str) -> bool:
    """True if `token` is the same word as `rule_pattern` after deobfuscation."""
    return bool(
        rule_pattern
        and token
        and deobfuscate_token(rule_pattern) == deobfuscate_token(token)
    )


def phrase_variant_equivalent(rule_pattern: str, text_compact: str) -> bool:
    """True if the compacted rule phrase appears in the compacted text.

    Handles spaced/hyphenated/leetspeak variants of multi-word phrases.
    """
    compact_pattern = compact_text(rule_pattern)
    return bool(compact_pattern) and compact_pattern in text_compact
