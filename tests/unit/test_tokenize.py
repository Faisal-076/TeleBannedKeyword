from app.analysis.tokenize import extract_ngrams, extract_terms, tokenize_words


def test_words():
    assert tokenize_words("Hello, world! 123") == ["hello", "world", "123"]


def test_bigrams():
    assert extract_ngrams(["a", "b", "c"], 2) == ["a b", "b c"]


def test_trigrams():
    assert extract_ngrams(["a", "b", "c", "d"], 3) == ["a b c", "b c d"]


def test_short_ngram_returns_empty():
    assert extract_ngrams(["a"], 2) == []


def test_extract_terms_filters_short():
    terms = extract_terms("go to the market today", max_n=2)
    assert "market" in terms
    assert "today" in terms
    assert "to" not in terms
    assert "go to" not in terms  # contains short token


def test_extract_terms_bigram():
    terms = extract_terms("free crypto giveaway", max_n=2)
    assert "free crypto" in terms
