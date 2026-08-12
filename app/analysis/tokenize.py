"""Tokenization and n-gram extraction over the normalized text."""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def tokenize_words(text: str) -> list[str]:
    return [m.lower() for m in _WORD_RE.findall(text)]


def extract_ngrams(tokens: list[str], n: int) -> list[str]:
    if n <= 0 or len(tokens) < n:
        return []
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def extract_terms(text: str, max_n: int = 2) -> set[str]:
    """Terms used for history indexing: unigrams (len>=3) and bigrams."""
    tokens = tokenize_words(text)
    terms: set[str] = set()
    for tok in tokens:
        if len(tok) >= 3:
            terms.add(tok)
    if max_n >= 2:
        for bi in extract_ngrams(tokens, 2):
            if all(len(t) >= 3 for t in bi.split(" ")):
                terms.add(bi)
    return terms
