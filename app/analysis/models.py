"""Shared data model for analysis results (also serialized into PostgreSQL JSON)."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceType(str, enum.Enum):
    EXPLICIT_RULE = "explicit_rule"
    REGEX = "regex"
    EXACT_HISTORY_MATCH = "exact_history_match"
    NORMALIZED_HISTORY_MATCH = "normalized_history_match"
    FUZZY_HISTORY_MATCH = "fuzzy_history_match"
    FUZZY_RULE_MATCH = "fuzzy_rule_match"
    SEMANTIC_MATCH = "semantic_match"
    UNSEEN = "unseen"
    UNKNOWN = "unknown"


class HistoryState(str, enum.Enum):
    SEEN = "seen"
    UNSEEN = "unseen"
    UNKNOWN = "unknown"


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    VERY_HIGH = "VERY HIGH"


class ChatStatus(str, enum.Enum):
    OK = "ok"
    UNKNOWN = "unknown"
    ERROR = "error"


class RuleMatch(BaseModel):
    rule_id: int | None = None
    rule_kind: str  # exact | phrase | regex
    matched_text: str  # the text fragment found in the message
    category: str
    evidence: EvidenceType
    scope: str = "global"
    weight: float = 0.0
    note: str | None = None


class HistoryEvidence(BaseModel):
    state: HistoryState = HistoryState.UNKNOWN
    count: int = 0
    example_context: str | None = None
    note: str | None = None


class Finding(BaseModel):
    """One suspicious term/phrase with all evidence attached."""

    id: str = ""
    term: str  # surface form as written in the message
    category: str = "general"
    evidence: EvidenceType = EvidenceType.UNKNOWN
    history: HistoryEvidence = Field(default_factory=HistoryEvidence)
    risk: int = 0
    level: RiskLevel = RiskLevel.LOW
    reason: str = ""
    recommendation: str | None = None
    score_components: list[str] = Field(default_factory=list)


class ChatAnalysis(BaseModel):
    chat_id: int
    title: str = ""
    username: str | None = None
    chat_type: str = "unknown"
    status: ChatStatus = ChatStatus.OK
    error_code: str | None = None
    score: int | None = None
    level: RiskLevel | None = None
    findings: list[Finding] = Field(default_factory=list)
    coverage_state: str = "unknown"  # complete | partial | unknown
    indexed_count: int = 0
    coverage_note: str | None = None


class AnalysisOutcome(BaseModel):
    request_id: str
    original_text: str
    normalized_text: str
    overall_score: int = 0
    overall_level: RiskLevel = RiskLevel.LOW
    chats_checked: int = 0
    chats_ok: int = 0
    chats_unavailable: int = 0
    chat_results: list[ChatAnalysis] = Field(default_factory=list)
    global_summary: list[str] = Field(default_factory=list)
    global_recommendations: list[str] = Field(default_factory=list)
    ai_used: bool = False
    ai_confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)

    def to_storage(self) -> dict[str, Any]:
        return {
            "overall_score": self.overall_score,
            "overall_level": self.overall_level.value,
            "global_result": {
                "summary": self.global_summary,
                "recommendations": self.global_recommendations,
                "ai_used": self.ai_used,
                "ai_confidence": self.ai_confidence,
                "warnings": self.warnings,
            },
            "chat_results": [c.model_dump(mode="json") for c in self.chat_results],
        }
