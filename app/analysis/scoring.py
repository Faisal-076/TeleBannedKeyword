"""Transparent, configurable risk scoring.

Score = weighted sum of evidence contributions, capped to [0, 100], with a
floor for deterministic evidence (explicit rules / regex) and a negative
discount for frequent historical usage. Every score carries human-readable
components so the result is always explainable.

Levels:  0-19 LOW | 20-39 MODERATE | 40-69 ELEVATED | 70-89 HIGH | 90-100 VERY HIGH
"""

from __future__ import annotations

from app.analysis.models import EvidenceType, Finding, HistoryState, RiskLevel
from app.config import RiskWeights

LEVEL_THRESHOLDS = (
    (RiskLevel.LOW, 20),
    (RiskLevel.MODERATE, 40),
    (RiskLevel.ELEVATED, 70),
    (RiskLevel.HIGH, 90),
    (RiskLevel.VERY_HIGH, 101),
)


def level_of(score: int) -> RiskLevel:
    if score < 0:
        score = 0
    if score > 100:
        score = 100
    for level, upper in LEVEL_THRESHOLDS:
        if score < upper:
            return level
    return RiskLevel.VERY_HIGH


def _base_weight(evidence: EvidenceType, weights: RiskWeights) -> float:
    return {
        EvidenceType.EXPLICIT_RULE: weights.explicit_rule,
        EvidenceType.REGEX: weights.regex,
        EvidenceType.FUZZY_RULE_MATCH: weights.fuzzy,
        EvidenceType.SEMANTIC_MATCH: weights.ai,
        EvidenceType.UNSEEN: weights.unseen,
        EvidenceType.UNKNOWN: weights.unseen * 0.5,
        EvidenceType.FUZZY_HISTORY_MATCH: weights.fuzzy,
        EvidenceType.EXACT_HISTORY_MATCH: 0.0,
        EvidenceType.NORMALIZED_HISTORY_MATCH: 0.0,
    }.get(evidence, 0.0)


def score_finding(finding: Finding, weights: RiskWeights | None = None) -> Finding:
    """Compute risk + level for a finding, mutating it in place."""
    if weights is None:
        from app.config import get_settings

        weights = get_settings().risk_weights

    components: list[str] = []
    score = _base_weight(finding.evidence, weights)

    if finding.evidence in (EvidenceType.EXPLICIT_RULE, EvidenceType.REGEX):
        components.append("explicit deterministic rule")
    if finding.evidence == EvidenceType.FUZZY_RULE_MATCH:
        components.append("similar restricted wording detected")

    if finding.history.state == HistoryState.SEEN:
        if finding.history.count >= 5:
            score += weights.frequent_use
            components.append(f"frequently used historically ({finding.history.count} occurrences)")
        else:
            score += weights.rare
            components.append(f"rare historical usage ({finding.history.count} occurrences)")
    elif finding.history.state == HistoryState.UNSEEN:
        score += weights.unseen
        components.append("0 historical occurrences in available history")
    elif finding.history.state == HistoryState.UNKNOWN:
        score += weights.unseen * 0.5
        components.append("historical coverage unavailable — treated as unknown")

    # Floor for deterministic evidence: a configured rule stays high risk
    # even if the phrase was previously used in the chat.
    if finding.evidence in (EvidenceType.EXPLICIT_RULE, EvidenceType.REGEX):
        score = max(score, weights.explicit_floor)
        components.append("configured rule overrides historical usage")

    score = max(0.0, min(100.0, score))
    finding.risk = int(round(score))
    finding.level = level_of(finding.risk)
    finding.score_components = components
    return finding
