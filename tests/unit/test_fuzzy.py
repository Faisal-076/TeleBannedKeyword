from app.analysis.fuzzy import best_fuzzy_match, obfuscation_equivalent, similarity


def test_similarity_identical():
    assert similarity("bitcoin", "bitcoin") == 1.0


def test_similarity_close():
    assert similarity("bitcoin", "bitecoin") > 0.8


def test_best_fuzzy_match_found():
    best, score = best_fuzzy_match("bitecoin", ["bitcoin", "ethereum"], 0.8)
    assert best == "bitcoin"
    assert score >= 0.8


def test_best_fuzzy_match_below_threshold():
    best, score = best_fuzzy_match("apple", ["zebra", "orange", "phone"], 0.9)
    assert best is None
    assert score == 0.0


def test_best_fuzzy_match_empty():
    assert best_fuzzy_match("word", [], 0.8) == (None, 0.0)


def test_obfuscation_equivalent():
    assert obfuscation_equivalent("bitcoin", "b1tcoin") is True
    assert obfuscation_equivalent("bitcoin", "bitc0in") is True
    assert obfuscation_equivalent("bitcoin", "ethereum") is False
