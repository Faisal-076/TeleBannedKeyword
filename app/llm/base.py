"""LLM provider abstraction (secondary analysis layer only).

Primary evidence is always deterministic (rules / regex / history). The LLM
provides contextual suspicion + safe-rewrite suggestions. It receives only:
submitted text, suspicious snippets, explicit rule metadata, per-chat
coverage summary — never raw chat histories.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class LLMPhrase:
    phrase: str
    suspicious: bool
    confidence: float
    reason: str = ""
    suggestion: str | None = None


@dataclass
class LLMAnalysis:
    phrases: list[LLMPhrase] = field(default_factory=list)
    overall_note: str | None = None


class LLMProvider(abc.ABC):
    name: str = "abstract"

    @property
    @abc.abstractmethod
    def enabled(self) -> bool: ...

    @abc.abstractmethod
    async def analyze(self, message_text: str, context: dict) -> LLMAnalysis: ...


class DisabledProvider(LLMProvider):
    name = "disabled"

    @property
    def enabled(self) -> bool:
        return False

    async def analyze(self, message_text: str, context: dict) -> LLMAnalysis:
        return LLMAnalysis()
