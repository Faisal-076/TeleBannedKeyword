"""Automatic whitespace removal for ordinary bot text messages."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import Message

router = Router(name="text_transform")


def transform_message_text(text: str) -> str:
    """Join words with readable casing while preserving non-whitespace characters."""
    transformed_tokens: list[str] = []
    seen_word = False
    previous_word_was_acronym = False

    for token in text.split():
        cased_characters = [character for character in token if _is_cased(character)]
        if not cased_characters:
            transformed_tokens.append(token)
            continue

        protected = _is_protected_token(token)
        is_acronym = not protected and all(character.isupper() for character in cased_characters)
        if protected or is_acronym:
            transformed = token
        else:
            capitalize = not seen_word or not previous_word_was_acronym
            transformed = _normalize_token_case(token, capitalize=capitalize)

        transformed_tokens.append(transformed)
        seen_word = True
        previous_word_was_acronym = is_acronym

    return "".join(transformed_tokens)


def _is_cased(character: str) -> bool:
    return character.islower() or character.isupper()


def _is_protected_token(token: str) -> bool:
    lowered = token.casefold()
    return (
        "://" in token
        or "@" in token
        or token.startswith("#")
        or lowered.startswith(("www.", "t.me/"))
    )


def _normalize_token_case(token: str, *, capitalize: bool) -> str:
    transformed: list[str] = []
    found_first_cased_character = False

    for character in token:
        if not _is_cased(character):
            transformed.append(character)
            continue
        if not found_first_cased_character:
            transformed.append(character.upper() if capitalize else character.lower())
            found_first_cased_character = True
        else:
            transformed.append(character.lower())

    return "".join(transformed)


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
)
async def on_ordinary_text(message: Message) -> None:
    transformed = transform_message_text(message.text or "")
    if transformed:
        await message.answer(transformed, parse_mode=None)
