"""History coverage estimation.

An incomplete index is NEVER treated as complete. `UNKNOWN` history means
"no evidence available", not "never used".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.database.models import SyncState, TargetChat


class CoverageState(str):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass
class ChatCoverage:
    state: str = CoverageState.UNKNOWN
    indexed_count: int = 0
    estimated_total: int | None = None
    ratio: float | None = None
    note: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.state == CoverageState.COMPLETE


def compute_coverage(chat: TargetChat) -> ChatCoverage:
    indexed = chat.sync_indexed_count or 0
    estimate = chat.sync_estimate
    state = chat.sync_state

    if state in (SyncState.DONE.value, SyncState.PARTIAL.value, SyncState.RUNNING.value):
        if estimate and estimate > 0:
            ratio = indexed / estimate
            if ratio >= 0.9:
                return ChatCoverage(
                    CoverageState.COMPLETE, indexed, estimate, round(ratio, 3),
                    "History fully indexed.",
                )
            return ChatCoverage(
                CoverageState.PARTIAL, indexed, estimate, round(ratio, 3),
                "Historical coverage is incomplete: only "
                f"{indexed} of ~{estimate} messages indexed.",
            )
        if indexed > 0:
            return ChatCoverage(
                CoverageState.PARTIAL, indexed, None, None,
                "Historical coverage is incomplete (total size unknown).",
            )
    if state == SyncState.FAILED.value:
        return ChatCoverage(
            CoverageState.UNKNOWN, indexed, estimate, None,
            "History sync failed; coverage unknown.",
        )
    return ChatCoverage(
        CoverageState.UNKNOWN, indexed, estimate, None,
        "Chat history has not been indexed yet.",
    )
