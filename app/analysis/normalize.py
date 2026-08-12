"""Text normalization.

Always preserves the original text. Produces multiple derived views used by
different stages of the pipeline:

- clean: NFC + zero-width removal + unicode punctuation folding + casefold +
  whitespace collapse. Used for exact/phrase/regex matching.
- deobfuscated: per-token leetspeak/confusable/repeat-char folding. Used for
  fuzzy matching against rule terms.
- compact: all non-alphanumeric removed. Used for spaced/hyphenated phrase
  variants like "w-o-r-d" / "w o r d".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_SPACES = re.compile(r"\s+")
_PUNCT_FOLD = str.maketrans(
    {"\u2013": "- ", "\u2014": "- ", "\u2010": "-", "\u2212": "-",
     "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
     "\u2026": "...", "\u00a0": " "}
)

# Unicode confusables: common homoglyphs folded to their ASCII counterpart.
CONFUSABLES: dict[str, str] = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ı": "i", "ſ": "s", "ν": "v", "ω": "w", "г": "r", "т": "t",
    "ъ": "b", "ь": "b", "б": "6", "ј": "j", "ѕ": "s", "ᴅ": "d", "Ʌ": "v", "м": "m",
}

# Leetspeak substitutions (applied per token).
LEET: dict[str, str] = {
    "4": "a", "@": "a", "8": "b", "3": "e", "6": "g", "9": "g", "1": "i",
    "!": "i", "0": "o", "7": "t", "5": "s", "$": "s", "2": "z", "+": "t",
    "|": "i", "_": "", "-": "",
}

_REPEAT = re.compile(r"(.)\1{2,}")
_NON_ALNUM = re.compile(r"[^\w]", re.UNICODE)


@dataclass
class NormalizedDocument:
    original: str
    clean: str
    deobfuscated: str
    compact: str
    tokens: list[str] = field(default_factory=list)
    bigrams: list[str] = field(default_factory=list)
    trigrams: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, str | list[str]]:
        return {
            "original": self.original,
            "clean": self.clean,
            "deobfuscated": self.deobfuscated,
            "compact": self.compact,
            "tokens": self.tokens,
            "bigrams": self.bigrams,
            "trigrams": self.trigrams,
        }


def strip_zero_width(text: str) -> str:
    return _ZERO_WIDTH.sub("", text)


def fold_punctuation(text: str) -> str:
    return text.translate(_PUNCT_FOLD)


def fold_confusables(text: str) -> str:
    return "".join(CONFUSABLES.get(ch, ch) for ch in text)


def fold_leet(text: str) -> str:
    return "".join(LEET.get(ch, ch) for ch in text)


def collapse_repeats(token: str) -> str:
    return _REPEAT.sub(r"\1\1", token)


def deobfuscate_token(token: str) -> str:
    folded = fold_leet(token)
    folded = fold_confusables(folded)
    return collapse_repeats(folded)


def normalize_text(text: str) -> str:
    """The 'clean' view used for exact/phrase/regex matching."""
    text = unicodedata.normalize("NFC", text)
    text = strip_zero_width(text)
    text = fold_punctuation(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = _SPACES.sub(" ", text)
    return text.strip()


def deobfuscate_text(text: str) -> str:
    return " ".join(
        deobfuscate_token(tok)
        for tok in re.findall(r"[\w]+", text, flags=re.UNICODE)
        if deobfuscate_token(tok)
    )


def compact_text(text: str) -> str:
    """Remove all non-alphanumeric characters, casefolded."""
    return normalize_text(_NON_ALNUM.sub("", text))


def normalize_document(text: str) -> NormalizedDocument:
    from app.analysis.tokenize import extract_ngrams

    clean = normalize_text(text)
    doc = NormalizedDocument(
        original=text,
        clean=clean,
        deobfuscated=deobfuscate_text(clean),
        compact=compact_text(clean),
    )
    tokens = [t for t in clean.split(" ") if t]
    doc.tokens = tokens
    doc.bigrams = extract_ngrams(tokens, 2)
    doc.trigrams = extract_ngrams(tokens, 3)
    return doc
