"""Multi-stage analysis pipeline.

Stage 1 normalization → stage 2 tokenization → stage 3 rule engine →
history evidence → fuzzy matching → optional LLM → risk scoring.

Every target chat is analyzed independently; one failing chat never fails
the whole request (its status is marked ERROR/UNKNOWN).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.analysis.models import (
    AnalysisOutcome,
    ChatAnalysis,
    ChatStatus,
    EvidenceType,
    Finding,
    HistoryState,
    RiskLevel,
)
from app.analysis.normalize import normalize_document
from app.analysis.scoring import level_of, score_finding
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import Rule, TargetChat
from app.history.coverage import compute_coverage
from app.history.searcher import HistorySearcher
from app.llm.base import LLMProvider
from app.rules.engine import fuzzy_rule_matches, match_rules
from app.telegram.errors import TelegramAccessError
from app.telegram.gateway import TelegramGateway

logger = logging.getLogger("app.analysis.pipeline")

MAX_FINDINGS_PER_CHAT = 8
UNSEEN_MIN_TOKEN_LEN = 6

_COMMON_WORDS = {
    "about", "above", "after", "again", "against", "already", "always", "another",
    "anyone", "anything", "around", "because", "before", "being", "below",
    "between", "bought", "brought", "cannot", "change", "check", "could",
    "course", "current", "didn't", "different", "doesn't", "doing", "during",
    "either", "enough", "every", "everyone", "everything", "example", "first",
    "follow", "following", "found", "getting", "going", "happened", "having",
    "hello", "here's", "however", "hours", "issue", "issues", "knows", "little",
    "looking", "maybe", "might", "minutes", "morning", "mostly", "never",
    "nothing", "nowhere", "number", "often", "people", "person", "please",
    "pretty", "probably", "quite", "really", "right", "saying", "school",
    "second", "should", "slightly", "someone", "something", "sometimes",
    "sorry", "still", "sure", "thanks", "thank", "that's", "their", "them",
    "then", "there", "these", "thing", "things", "think", "thought", "three",
    "through", "time", "together", "took", "trying", "unless", "until",
    "wanted", "welcome", "whether", "which", "while", "whole", "within",
    "without", "would", "years", "you're", "your", "you've", "going", "today",
    "tomorrow", "yesterday", "night", "weekend", "message", "messages",
}


@dataclass
class PipelineDeps:
    gateway: TelegramGateway
    searcher: HistorySearcher
    llm: LLMProvider | None = None


class AnalysisPipeline:
    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps
        self._settings = get_settings()

    async def analyze(self, request_text: str) -> AnalysisOutcome:
        doc = normalize_document(request_text)
        chats = await self._enabled_chats()
        outcome = AnalysisOutcome(
            request_id="",
            original_text=request_text,
            normalized_text=doc.clean,
        )

        if not chats:
            outcome.warnings.append("No target chats configured. Add one with /addchat.")
            return outcome

        all_rules = await self._all_rules()
        global_rules = [r for r in all_rules if r.scope == "global"]
        ai_results = None
        if self._deps.llm is not None and self._deps.llm.enabled:
            ai_results = await self._deps.llm.analyze(request_text, context={})

        for chat in chats:
            chat_result = await self._analyze_chat(chat, doc, global_rules, all_rules, outcome)
            outcome.chat_results.append(chat_result)

        outcome.chats_checked = len(chats)
        outcome.chats_ok = sum(1 for c in outcome.chat_results if c.status == ChatStatus.OK)
        outcome.chats_unavailable = sum(
            1 for c in outcome.chat_results if c.status != ChatStatus.OK
        )

        if ai_results is not None:
            self._merge_ai(outcome, ai_results)

        scores = [c.score for c in outcome.chat_results if c.score is not None]
        outcome.overall_score = max(scores) if scores else 0
        outcome.overall_level = level_of(outcome.overall_score)
        outcome.ai_used = ai_results is not None and bool(ai_results.phrases)
        if ai_results is not None and ai_results.phrases:
            outcome.ai_confidence = max(p.confidence for p in ai_results.phrases)
        outcome.global_summary = self._global_summary(outcome)
        outcome.global_recommendations = self._global_recommendations(outcome)
        if any(c.coverage_state != "complete" for c in outcome.chat_results):
            outcome.warnings.append(
                "Some chats have incomplete history coverage — UNSEEN results "
                "there are reported as UNKNOWN, never as banned."
            )
        return outcome

    # ------------------------------------------------------------------ internals

    async def _enabled_chats(self) -> list[TargetChat]:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).where(TargetChat.enabled.is_(True)).order_by(TargetChat.id)
            )
            return list(result.scalars().all())

    async def _all_rules(self) -> list[Rule]:
        async with session_scope() as session:
            result = await session.execute(select(Rule).where(Rule.enabled.is_(True)))
            return list(result.scalars().all())

    async def _analyze_chat(self, chat, doc, global_rules, all_rules, outcome) -> ChatAnalysis:
        coverage = compute_coverage(chat)
        result = ChatAnalysis(
            chat_id=chat.telegram_chat_id,
            title=chat.title,
            username=chat.username,
            chat_type=chat.chat_type,
            coverage_state=coverage.state,
            indexed_count=coverage.indexed_count,
            coverage_note=coverage.note,
        )
        if chat.access_state != "accessible":
            result.status = ChatStatus.UNKNOWN
            result.error_code = f"chat_not_accessible:{chat.access_state}"
            return result
        try:
            chat_rules = [r for r in all_rules if r.scope == "chat" and r.chat_id == chat.telegram_chat_id]
            rules = global_rules + chat_rules
            findings = self._rule_findings(doc, rules)
            self._unseen_scan(chat, doc, findings)
            await self._history_evidence(chat, findings)
            for f in findings:
                score_finding(f)
            findings.sort(key=lambda f: f.risk, reverse=True)
            result.findings = findings[:MAX_FINDINGS_PER_CHAT]
            if result.findings:
                result.score = result.findings[0].risk
                result.level = result.findings[0].level
            else:
                result.score = 0
                result.level = RiskLevel.LOW
            return result
        except TelegramAccessError as exc:
            result.status = ChatStatus.ERROR
            result.error_code = exc.code
            result.coverage_note = "analysis failed for this chat"
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pipeline: chat analysis crashed chat=%s err=%s", chat.telegram_chat_id, type(exc).__name__,
                exc_info=True,
            )
            result.status = ChatStatus.ERROR
            result.error_code = type(exc).__name__
            result.coverage_note = "analysis failed for this chat"
            return result

    def _rule_findings(self, doc, rules) -> list[Finding]:
        findings: list[Finding] = []
        for match in match_rules(doc, rules):
            term = self._surface_form(doc, match.matched_text)
            if any(f.term == term for f in findings):
                continue
            findings.append(
                Finding(
                    term=term,
                    category=match.category,
                    evidence=match.evidence,
                    reason=self._reason_for(match.evidence),
                    recommendation=match.note,
                )
            )
        for match in fuzzy_rule_matches(doc, rules):
            term = self._surface_form(doc, match.matched_text)
            if any(f.term == term for f in findings):
                continue
            findings.append(
                Finding(
                    term=term,
                    category=match.category,
                    evidence=match.evidence,
                    reason=self._reason_for(match.evidence),
                    recommendation=match.note,
                )
            )
        return findings

    async def _history_evidence(self, chat, findings: list[Finding]) -> None:
        for finding in findings:
            evidence, fuzzy_term = await self._deps.searcher.evidence_for_term(
                chat, finding.term
            )
            finding.history = evidence
            if fuzzy_term and evidence.note:
                finding.history.note = evidence.note
                finding.reason = "Similar wording observed historically; verify intent."
            if evidence.state == HistoryState.UNSEEN:
                finding.reason = "Not observed in the available chat history (unusual/unfamiliar wording)."
            elif evidence.state == HistoryState.UNKNOWN:
                finding.reason = "Historical coverage unavailable — treated as unknown."
            elif evidence.state == HistoryState.SEEN:
                # The term exists in history: a novelty flag is no longer
                # justified — the finding (if it stays) is re-scored by
                # frequent/rare usage instead of unseen-ness.
                finding.reason = "Term has appeared in chat history; novelty flag dropped."

    def _unseen_scan(self, chat, doc, findings: list[Finding]) -> None:
        covered = {f.term.casefold() for f in findings}
        candidates: list[str] = []
        for token in doc.tokens:
            if not self._unseen_candidate(token):
                continue
            if token in covered:
                continue
            candidates.append(token)
            covered.add(token)
        for bi in doc.bigrams:
            words = bi.split(" ")
            if len(words) != 2 or not all(self._unseen_candidate(w) for w in words):
                continue
            if bi in covered:
                continue
            candidates.append(bi)
            covered.add(bi)
        if len(candidates) > MAX_FINDINGS_PER_CHAT:
            candidates = sorted(candidates, key=len, reverse=True)[:MAX_FINDINGS_PER_CHAT]
        for candidate in candidates:
            findings.append(
                Finding(
                    term=candidate,
                    category="history",
                    evidence=EvidenceType.UNSEEN,
                    reason="Not observed in the available chat history (unusual/unfamiliar wording).",
                )
            )

    @staticmethod
    def _unseen_candidate(token: str) -> bool:
        """Novelty candidates only: real words, no codes/links/mentions.

        Filters cut most false positives: numbers/codes, URLs, mentions,
        hashtags, punctuation and pure-symbol tokens never become UNSEEN
        findings.
        """
        if len(token) < UNSEEN_MIN_TOKEN_LEN:
            return False
        if not any(ch.isalpha() for ch in token):
            return False
        if any(ch.isdigit() for ch in token):
            return False
        if token in _COMMON_WORDS:
            return False
        if token.startswith(("@", "#")):
            return False
        if any(ch in token for ch in ("/", "\\", ".", ",", ":", ";")):
            return False
        return True

    def _merge_ai(self, outcome: AnalysisOutcome, ai: object) -> None:
        from app.llm.base import LLMAnalysis

        assert isinstance(ai, LLMAnalysis)
        flagged = [p for p in ai.phrases if p.suspicious and p.confidence >= 0.5 and p.phrase]
        if not flagged:
            return
        chat_results = [c for c in outcome.chat_results if c.status == ChatStatus.OK]
        if not chat_results:
            return
        for phrase in flagged:
            key = phrase.phrase.casefold()
            attached = False
            for chat_result in chat_results:
                for finding in chat_result.findings:
                    if key in finding.term.casefold() or finding.term.casefold() in key:
                        finding.recommendation = phrase.suggestion or finding.recommendation
                        if not finding.reason.startswith("AI"):
                            finding.reason = f"AI contextual suspicion ({phrase.confidence:.2f}): {phrase.reason}"
                        score_finding(finding)
                        attached = True
                        break
                if attached:
                    break
            if not attached and key in outcome.original_text.casefold():
                target = chat_results[0]
                finding = Finding(
                    term=phrase.phrase,
                    category="semantic",
                    evidence=EvidenceType.SEMANTIC_MATCH,
                    reason=f"AI contextual suspicion ({phrase.confidence:.2f}): {phrase.reason}",
                    recommendation=phrase.suggestion,
                )
                score_finding(finding)
                target.findings.append(finding)
                target.findings.sort(key=lambda f: f.risk, reverse=True)
                target.findings = target.findings[:MAX_FINDINGS_PER_CHAT]
                target.score = target.findings[0].risk
                target.level = target.findings[0].level
            outcome.global_summary.append(
                f"AI: '{phrase.phrase}' flagged (confidence {phrase.confidence:.2f})."
            )

    def _surface_form(self, doc, matched_text: str) -> str:
        if matched_text and matched_text.casefold() in doc.original.casefold():
            return matched_text
        return matched_text

    def _reason_for(self, evidence: EvidenceType) -> str:
        return {
            EvidenceType.EXPLICIT_RULE: "Matches a configured restricted term.",
            EvidenceType.REGEX: "Matches a configured restricted pattern (regex).",
            EvidenceType.FUZZY_RULE_MATCH: "Similar to a configured restricted term.",
            EvidenceType.UNSEEN: "Not observed in the available chat history (unusual/unfamiliar wording).",
            EvidenceType.UNKNOWN: "Historical evidence unavailable.",
            EvidenceType.SEMANTIC_MATCH: "AI contextual suspicion.",
        }.get(evidence, "")

    def _global_summary(self, outcome: AnalysisOutcome) -> list[str]:
        lines = []
        if outcome.chats_ok == outcome.chats_checked and outcome.chats_checked:
            lines.append(f"All {outcome.chats_checked} target chats analyzed.")
        else:
            lines.append(f"{outcome.chats_ok}/{outcome.chats_checked} chats analyzed; {outcome.chats_unavailable} unavailable.")
        top = None
        for c in outcome.chat_results:
            for f in c.findings:
                if top is None or f.risk > top[2].risk:
                    top = (c.title, c.chat_id, f)
        if top:
            lines.append(
                f"Strongest signal: '{top[2].term}' in {top[0]} ({top[2].level.value})."
            )
        return lines

    def _global_recommendations(self, outcome: AnalysisOutcome) -> list[str]:
        recs: list[str] = []
        for c in outcome.chat_results:
            for f in c.findings:
                if f.recommendation and f.recommendation not in recs:
                    recs.append(f.recommendation)
            if len(recs) >= 3:
                break
        return recs
