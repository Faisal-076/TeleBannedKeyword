from app.analysis.models import EvidenceType, Finding, HistoryEvidence, HistoryState, RiskLevel
from app.analysis.scoring import level_of, score_finding


def test_level_boundaries():
    assert level_of(0) == RiskLevel.LOW
    assert level_of(19) == RiskLevel.LOW
    assert level_of(20) == RiskLevel.MODERATE
    assert level_of(39) == RiskLevel.MODERATE
    assert level_of(40) == RiskLevel.ELEVATED
    assert level_of(69) == RiskLevel.ELEVATED
    assert level_of(70) == RiskLevel.HIGH
    assert level_of(89) == RiskLevel.HIGH
    assert level_of(90) == RiskLevel.VERY_HIGH


def test_explicit_rule_scores_high_with_floor():
    finding = Finding(
        term="scam",
        evidence=EvidenceType.EXPLICIT_RULE,
        history=HistoryEvidence(state=HistoryState.SEEN, count=50),
    )
    score_finding(finding)
    assert finding.risk >= 70
    assert finding.level == RiskLevel.HIGH
    assert any("floor" in c or "rule" in c for c in finding.score_components)


def test_explicit_rule_without_history():
    finding = Finding(
        term="scam",
        evidence=EvidenceType.EXPLICIT_RULE,
        history=HistoryEvidence(state=HistoryState.UNKNOWN),
    )
    score_finding(finding)
    assert finding.risk >= 70


def test_unseen_is_small_risk_not_banned():
    finding = Finding(
        term="someword",
        evidence=EvidenceType.UNSEEN,
        history=HistoryEvidence(state=HistoryState.UNSEEN, count=0),
    )
    score_finding(finding)
    assert 0 < finding.risk < 40
    assert finding.level in (RiskLevel.LOW, RiskLevel.MODERATE)


def test_frequent_historical_usage_lowers_risk():
    unseen = Finding(
        term="someword",
        evidence=EvidenceType.UNSEEN,
        history=HistoryEvidence(state=HistoryState.UNSEEN, count=0),
    )
    seen = Finding(
        term="someword",
        evidence=EvidenceType.UNSEEN,
        history=HistoryEvidence(state=HistoryState.SEEN, count=25),
    )
    score_finding(unseen)
    score_finding(seen)
    assert seen.risk < unseen.risk
    assert any("frequently" in c for c in seen.score_components)


def test_unknown_history_no_assumption():
    finding = Finding(
        term="word",
        evidence=EvidenceType.UNSEEN,
        history=HistoryEvidence(state=HistoryState.UNKNOWN, count=0),
    )
    score_finding(finding)
    assert 0 < finding.risk < 40
    assert finding.risk < 20 or finding.risk >= 0


def test_regex_evidence_high():
    finding = Finding(term="x", evidence=EvidenceType.REGEX, history=HistoryEvidence(state=HistoryState.UNKNOWN))
    score_finding(finding)
    assert finding.risk >= 70


def test_score_clamped():
    finding = Finding(
        term="x",
        evidence=EvidenceType.EXPLICIT_RULE,
        history=HistoryEvidence(state=HistoryState.UNKNOWN),
        risk=200,
    )
    score_finding(finding)
    assert finding.risk <= 100
