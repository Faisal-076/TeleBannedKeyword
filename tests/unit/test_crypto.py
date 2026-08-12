from app.security.crypto import CryptoError, decrypt_string, encrypt_string, derive_key


MASTER = "correct horse battery staple"


def test_roundtrip():
    blob = encrypt_string("session-secret-data", MASTER)
    assert blob.startswith("v1:")
    assert decrypt_string(blob, MASTER) == "session-secret-data"


def test_wrong_key_fails():
    blob = encrypt_string("secret", MASTER)
    try:
        decrypt_string(blob, "wrong-key")
    except CryptoError:
        return
    raise AssertionError("decrypt with wrong key must raise CryptoError")


def test_tampered_blob_fails():
    blob = encrypt_string("secret", MASTER)
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    try:
        decrypt_string(tampered, MASTER)
    except CryptoError:
        return
    raise AssertionError("tampered ciphertext must fail authentication")


def test_key_derivation_differs():
    assert derive_key("a") != derive_key("b")


def test_blob_contains_no_plaintext():
    blob = encrypt_string("super-secret-session", MASTER)
    assert "super-secret-session" not in blob
