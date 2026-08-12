from app.analysis.normalize import (
    compact_text,
    deobfuscate_text,
    deobfuscate_token,
    normalize_document,
    normalize_text,
    strip_zero_width,
)


def test_original_preserved():
    doc = normalize_document("Hello  World  123")
    assert doc.original == "Hello  World  123"


def test_casefold_and_whitespace():
    assert normalize_text("  HELLO   World  ") == "hello world"


def test_zero_width_removed():
    assert strip_zero_width("a\u200bb\u200cc") == "abc"


def test_punctuation_folding():
    assert normalize_text("hello—world …ok") == "hello- world ...ok"


def test_unicode_normalization():
    assert normalize_text("caf\u00e9") == "café"


def test_leetspeak_deobfuscation():
    assert deobfuscate_token("w0rd") == "word"
    assert deobfuscate_token("fr33") == "free"


def test_confusables():
    assert deobfuscate_token("работа") == "pa6ota"  # cyrillic р/а and leet 6->g? no: 6 not present


def test_confusables_cyrillic():
    assert deobfuscate_token("рa") == "pa"


def test_repeated_chars_collapse():
    assert deobfuscate_token("coooool") == "cool"


def test_compact_text():
    assert compact_text("w-o-r-d") == "word"
    assert compact_text("w o r d") == "word"


def test_deobfuscated_document_view():
    doc = normalize_document("buy fr33 4pp")
    assert doc.deobfuscated == "buy free app"
