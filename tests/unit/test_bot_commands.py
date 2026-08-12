"""Regression coverage for bot command response formatting."""

from __future__ import annotations

from html import unescape

from app.bot.handlers.commands import HELP_TEXT, cmd_help, cmd_start


class _MessageRecorder:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


async def test_start_and_help_send_html_safe_shared_help_text():
    """Literal command syntax must not become Telegram HTML markup."""
    start_message = _MessageRecorder()
    help_message = _MessageRecorder()

    await cmd_start(start_message)  # type: ignore[arg-type]
    await cmd_help(help_message)  # type: ignore[arg-type]

    assert help_message.answers == [HELP_TEXT]
    assert start_message.answers == ["Welcome to the Telegram Message Analyzer.\n\n" + HELP_TEXT]
    assert "<" not in HELP_TEXT
    assert "<@username|t.me link|invite link|chat id>" in unescape(HELP_TEXT)
