from electridrive import licensing


def test_sign_verify_roundtrip(monkeypatch):
    priv, pub = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", pub)
    key = licensing.sign("Jane Doe", "jane@example.com", priv)
    lic = licensing.verify(key)
    assert lic is not None
    assert lic.name == "Jane Doe"
    assert lic.email == "jane@example.com"
    assert lic.edition == "pro"
    assert licensing.is_valid(key)


def test_tampered_key_is_invalid(monkeypatch):
    priv, pub = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", pub)
    key = licensing.sign("J", "j@x.com", priv)
    assert licensing.verify(key[:-4] + "AAAA") is None


def test_key_from_other_signer_is_invalid(monkeypatch):
    priv1, pub1 = licensing.generate_keypair()
    priv2, _ = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", pub1)
    key = licensing.sign("J", "j@x.com", priv2)  # signed by the wrong key
    assert licensing.verify(key) is None


def test_garbage_and_empty_invalid():
    assert licensing.is_valid("") is False
    assert licensing.verify("not-a-key") is None
    assert licensing.verify("a.b") is None
