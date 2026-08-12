"""Result message formatting + inline keyboards.

Every message stays under Telegram's 4096-char limit; full evidence is
delivered on demand via the "Show Evidence" button.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.analysis.models import AnalysisOutcome, ChatStatus, Finding

MAX_MAIN_FINDINGS = 4
MAX_CHATS_IN_MAIN = 6
MAX_CHAT_FINDINGS_EVIDENCE = 8
EVIDENCE_CHUNK = 3900

_MATCH_LABEL = {
    "explicit_rule": "Explicit rule",
    "regex": "Regex",
    "fuzzy_rule_match": "Fuzzy (rule)",
    "exact_history_match": "History (exact)",
    "normalized_history_match": "History (normalized)",
    "fuzzy_history_match": "History (fuzzy)",
    "semantic_match": "AI contextual",
    "unseen": "Unseen",
    "unknown": "Unknown",
}

_HISTORY_LABEL = {
    "seen": "Seen",
    "unseen": "Not seen",
    "unknown": "Unknown",
}


def format_result(outcome: AnalysisOutcome) -> str:
    lines: list[str] = ["⚠️ Message Analysis", ""]
    lines.append(f"Risk: {outcome.overall_level.value} — {outcome.overall_score}/100")
    lines.append("")
    lines.append(f"Target chats checked: {outcome.chats_checked}")
    lines.append(f"Chats successfully analyzed: {outcome.chats_ok}")
    lines.append(f"Chats unavailable: {outcome.chats_unavailable}")
    lines.append("")

    all_findings: list[tuple[Finding, str]] = []
    for chat in outcome.chat_results:
        for finding in chat.findings:
            all_findings.append((finding, chat.title or f"chat {chat.chat_id}"))
    all_findings.sort(key=lambda item: item[0].risk, reverse=True)

    if all_findings:
        lines.append("Potentially problematic wording:")
        for idx, (finding, chat_title) in enumerate(all_findings[:MAX_MAIN_FINDINGS], 1):
            history = finding.history
            occurrences = (
                f"{history.count}"
                if history.state.value == "seen"
                else _HISTORY_LABEL.get(history.state.value, "unknown")
            )
            lines.append(f'{idx}. "{finding.term}"')
            lines.append(f"   Risk: {finding.level.value}")
            lines.append(f"   Match: {_MATCH_LABEL.get(finding.evidence.value, finding.evidence.value)}")
            lines.append(f"   Historical occurrences: {occurrences}")
            lines.append(f"   Reason: {finding.reason or '-'}")
            lines.append("")
        if len(all_findings) > MAX_MAIN_FINDINGS:
            lines.append(f"...and {len(all_findings) - MAX_MAIN_FINDINGS} more (see evidence).")
            lines.append("")
    else:
        lines.append("No potentially problematic wording detected.")
        lines.append("")

    recs = outcome.global_recommendations
    if recs:
        lines.append("Recommendation:")
        for rec in recs[:2]:
            lines.append(f"• {rec}")
        lines.append("")

    lines.append("Per-chat:")
    for chat in outcome.chat_results[:MAX_CHATS_IN_MAIN]:
        label = chat.title or f"chat {chat.chat_id}"
        if chat.status == ChatStatus.ERROR:
            level = "ERROR"
        elif chat.level is None:
            level = "UNKNOWN"
        else:
            level = chat.level.value
        lines.append(f"• {label}: {level}")
    if len(outcome.chat_results) > MAX_CHATS_IN_MAIN:
        lines.append(f"• ...and {len(outcome.chat_results) - MAX_CHATS_IN_MAIN} more chats.")

    text = "\n".join(lines)
    if len(text) > 4090:
        return text[: 4090 - 60] + "\n…(trimmed; use Show Evidence for full detail)"
    return text


def format_evidence(outcome: AnalysisOutcome) -> list[str]:
    """Full evidence per chat/finding, chunked for Telegram."""
    chunks: list[str] = []
    current: list[str] = ["📄 Full analysis evidence", ""]
    current.append(f"Request: {outcome.request_id}")
    current.append(f"Message: {outcome.original_text[:500]}")
    current.append("")

    for chat in outcome.chat_results:
        block = [f"Chat: {chat.title or chat.chat_id}", f"Status: {chat.status.value}"]
        if chat.coverage_note:
            block.append(f"Coverage: {chat.coverage_note}")
        if chat.score is not None:
            block.append(f"Chat risk: {chat.level.value} — {chat.score}/100")
        for finding in chat.findings[:MAX_CHAT_FINDINGS_EVIDENCE]:
            block.append("")
            block.append(f'• "{finding.term}" [{finding.category}]')
            block.append(f"  Evidence: {_MATCH_LABEL.get(finding.evidence.value, finding.evidence.value)}")
            block.append(f"  History: {_HISTORY_LABEL.get(finding.history.state.value, 'unknown')}"
                         f" (count: {finding.history.count})")
            if finding.history.example_context:
                block.append(f"  Example: {finding.history.example_context[:300]}")
            block.append(f"  Risk: {finding.level.value} — {finding.risk}/100")
            for comp in finding.score_components:
                block.append(f"  • {comp}")
            block.append(f"  Reason: {finding.reason or '-'}")
            if finding.recommendation:
                block.append(f"  Suggestion: {finding.recommendation}")
        block.append("")
        for line in block:
            current.append(line)
            if sum(len(x) for x in current) > EVIDENCE_CHUNK:
                chunks.append("\n".join(current))
                current = [""]

    if current and "".join(current).strip():
        chunks.append("\n".join(current))
    if not chunks:
        return ["No evidence recorded."]
    return chunks


def format_chats(outcome: AnalysisOutcome) -> str:
    lines = ["💬 Target chats summary", ""]
    for chat in outcome.chat_results:
        if chat.status == ChatStatus.ERROR:
            status = f"ERROR ({chat.error_code})"
        elif chat.level is None:
            status = "UNKNOWN"
        else:
            status = f"{chat.level.value} ({chat.score}/100)"
        lines.append(f"• {chat.title or 'chat ' + str(chat.chat_id)}: {status}")
        if chat.coverage_note:
            lines.append(f"  {chat.coverage_note}")
    text = "\n".join(lines)
    return text if len(text) <= 4090 else text[:4090] + "\n…(trimmed)"


def build_result_keyboard(request_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Edit Text", callback_data=f"edit:{request_id}"),
        InlineKeyboardButton(text="Recheck", callback_data=f"recheck:{request_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="Show Evidence", callback_data=f"evidence:{request_id}"),
        InlineKeyboardButton(text="Show Target Chats", callback_data=f"chats:{request_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="Cancel", callback_data=f"cancel:{request_id}"),
    )
    return builder.as_markup()


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Yes, revoke session", callback_data="confirm_logout"),
        InlineKeyboardButton(text="Cancel", callback_data="cancel_logout"),
    )
    return builder.as_markup()
