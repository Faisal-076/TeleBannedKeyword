"""Pipeline-level unit tests without a database: normalization + scoring."""

from app.analysis.models import EvidenceType, Finding, HistoryEvidence, HistoryState
from app.analysis.normalize import normalize_document
from app.analysis.scoring import score_finding


def test_document_normalization_views():
    doc = normalize_document("Buy fr33 4pp ⚠️")
    assert doc.original == "Buy fr33 4pp ⚠️"
    assert doc.deobfuscated == "buy free app"  # only alphanumeric tokens survive
    assert doc.tokens  # non-empty


def test_document_tokens_and_ngrams():
    doc = normalize_document("alpha beta gamma")
    assert doc.tokens == ["alpha", "beta", "gamma"]
    assert "alpha beta" in doc.bigrams
    assert "alpha beta gamma" in doc.trigrams


def test_evidence_scoring_chain():
    """Explicit rule + seen history must stay HIGH."""
    finding = Finding(
        term="scam",
        category="scam",
        evidence=EvidenceType.EXPLICIT_RULE,
        history=HistoryEvidence(state=HistoryState.SEEN, count=9),
    )
    score_finding(finding)
    assert finding.risk >= 70
    assert finding.level.value in ("HIGH", "VERY HIGH")


def test_unseen_never_claims_banned():
    finding = Finding(
        term="zorblax",
        evidence=EvidenceType.UNSEEN,
        history=HistoryEvidence(state=HistoryState.UNSEEN, count=0),
        reason="Not observed in the available chat history",
    )
    score_finding(finding)
    assert finding.risk < 40
    assert "banned" not in finding.reason.lower()
