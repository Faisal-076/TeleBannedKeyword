"""Concrete LLM providers: OpenAI-compatible chat completions + DeepSeek."""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.llm.base import LLMAnalysis, LLMPhrase, LLMProvider
from app.llm.prompts import build_messages

logger = logging.getLogger("app.llm")

DEEPSEEK_DEFAULT_URL = "https://api.deepseek.com"

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class OpenAICompatibleProvider(LLMProvider):
    """Generic OpenAI-compatible /chat/completions client."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_context_chars: int = 4000,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_context = max_context_chars

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._model)

    async def analyze(self, message_text: str, context: dict) -> LLMAnalysis:
        if not self.enabled:
            return LLMAnalysis()
        messages = build_messages(message_text[: self._max_context], context)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers
                )
                response.raise_for_status()
                data = response.json()
            content = data["choices"][0]["message"]["content"]
            return _parse_analysis(content)
        except httpx.HTTPError as exc:
            logger.warning("llm: request failed: %s", type(exc).__name__)
            return LLMAnalysis()
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("llm: response malformed: %s", type(exc).__name__)
            return LLMAnalysis()


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"

    def __init__(self, api_key: str, base_url: str | None, model: str, timeout: float, max_context_chars: int) -> None:
        super().__init__(
            base_url or DEEPSEEK_DEFAULT_URL,
            api_key,
            model or "deepseek-chat",
            timeout,
            max_context_chars,
        )


def create_provider(settings) -> LLMProvider:
    from app.llm.base import DisabledProvider

    provider = (settings.llm_provider or "disabled").lower()
    if provider == "disabled":
        return DisabledProvider()
    api_key = settings.llm_api_key.get_secret_value()
    if not api_key:
        logger.warning("llm: provider %s configured but LLM_API_KEY empty; disabled", provider)
        return DisabledProvider()
    kwargs = dict(
        api_key=api_key,
        base_url=settings.llm_base_url or None,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_context_chars=settings.llm_max_context_chars,
    )
    if provider == "deepseek":
        return DeepSeekProvider(**kwargs)
    if provider in ("openai", "openai_compatible"):
        base = kwargs.pop("base_url")
        return OpenAICompatibleProvider(base_url=base or "https://api.openai.com/v1", **kwargs)
    logger.warning("llm: unknown provider %r; disabled", provider)
    return DisabledProvider()


def _parse_analysis(content: str) -> LLMAnalysis:
    content = content.strip()
    fence = _JSON_FENCE_RE.match(content)
    if fence:
        content = fence.group(1)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json") from exc
    phrases = [
        LLMPhrase(
            phrase=str(item.get("phrase", "")),
            suspicious=bool(item.get("suspicious")),
            confidence=float(item.get("confidence", 0.0)),
            reason=str(item.get("reason", "")),
            suggestion=str(item.get("suggestion")) if item.get("suggestion") else None,
        )
        for item in payload.get("phrases", [])
        if item.get("phrase")
    ]
    return LLMAnalysis(phrases=phrases, overall_note=payload.get("overall_note"))
