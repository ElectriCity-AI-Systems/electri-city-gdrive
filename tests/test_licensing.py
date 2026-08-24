import json

import pytest

from electridrive import licensing


NEW_PUBLIC_KEY_B64 = "EU2jhjURq5JpCdRgsZlPTeAm-X5vl0pH4Mj67fiD3xE"
LEGACY_PUBLIC_KEY_B64 = "7uMvlZUF3pVw2Pu1rZ42G6Ko5_5Et4GnD7A7VguiP0g"


def _signed_payload(private_key, **overrides):
    data = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "edition": licensing.EDITION,
        "issued": 1_700_000_000,
    }
    data.update(overrides)
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{licensing._b64e(payload)}.{licensing._b64e(private_key.sign(payload))}"


def _use_ephemeral_embedded_keys(monkeypatch):
    current_private, current_public = licensing.generate_keypair()
    legacy_private, legacy_public = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", current_public)
    monkeypatch.setattr(licensing, "LEGACY_PUBLIC_KEY_B64", legacy_public)
    return (current_private, current_public), (legacy_private, legacy_public)


def test_embedded_public_keys_are_current_then_legacy():
    assert licensing.PUBLIC_KEY_B64 == NEW_PUBLIC_KEY_B64
    assert licensing.LEGACY_PUBLIC_KEY_B64 == LEGACY_PUBLIC_KEY_B64
    assert len(licensing._b64d(licensing.PUBLIC_KEY_B64)) == 32
    assert len(licensing._b64d(licensing.LEGACY_PUBLIC_KEY_B64)) == 32


def test_license_signed_by_current_key_pair_verifies(monkeypatch):
    (current_private, _), _ = _use_ephemeral_embedded_keys(monkeypatch)
    key = licensing.sign("Jane Doe", "jane@example.com", current_private)
    lic = licensing.verify(key)
    assert lic is not None
    assert lic.name == "Jane Doe"
    assert lic.email == "jane@example.com"
    assert lic.edition == "pro"
    assert licensing.is_valid(key)


def test_generated_legacy_style_key_verifies_when_explicit():
    legacy_private, legacy_public = licensing.generate_keypair()
    key = licensing.sign("Legacy User", "legacy@example.com", legacy_private)
    lic = licensing.verify(key, public_key_b64=legacy_public)
    assert lic is not None
    assert lic.name == "Legacy User"


def test_default_verification_accepts_current_and_legacy_keys(monkeypatch):
    (current_private, _), (legacy_private, _) = _use_ephemeral_embedded_keys(monkeypatch)
    current_key = licensing.sign("Current", "current@example.com", current_private)
    legacy_key = licensing.sign("Legacy", "legacy@example.com", legacy_private)

    assert licensing.verify(current_key).name == "Current"
    assert licensing.verify(legacy_key).name == "Legacy"
    assert licensing.is_valid(current_key)
    assert licensing.is_valid(legacy_key)


def test_default_verification_continues_past_a_malformed_current_key(monkeypatch):
    legacy_private, legacy_public = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", "not/base64!")
    monkeypatch.setattr(licensing, "LEGACY_PUBLIC_KEY_B64", legacy_public)
    key = licensing.sign("Legacy", "legacy@example.com", legacy_private)

    assert licensing.verify(key).name == "Legacy"


def test_explicit_public_key_is_exact_override(monkeypatch):
    (current_private, _), _ = _use_ephemeral_embedded_keys(monkeypatch)
    key = licensing.sign("Current", "current@example.com", current_private)
    _, wrong_public = licensing.generate_keypair()

    assert licensing.verify(key, public_key_b64=wrong_public) is None
    assert licensing.verify(key, public_key_b64="") is None


def test_tampered_payload_is_invalid():
    private, public = licensing.generate_keypair()
    key = licensing.sign("Jane", "jane@example.com", private, issued=1_700_000_000)
    payload_b64, signature_b64 = key.split(".")
    payload = json.loads(licensing._b64d(payload_b64))
    payload["name"] = "Mallory"
    tampered_payload = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    tampered_key = f"{licensing._b64e(tampered_payload)}.{signature_b64}"

    assert licensing.verify(tampered_key, public_key_b64=public) is None


def test_tampered_signature_is_invalid():
    private, public = licensing.generate_keypair()
    key = licensing.sign("Jane", "jane@example.com", private)
    payload_b64, signature_b64 = key.split(".")
    signature = bytearray(licensing._b64d(signature_b64))
    signature[-1] ^= 1
    tampered_key = f"{payload_b64}.{licensing._b64e(bytes(signature))}"

    assert licensing.verify(tampered_key, public_key_b64=public) is None


def test_wrong_key_is_invalid():
    private, _ = licensing.generate_keypair()
    _, wrong_public = licensing.generate_keypair()
    key = licensing.sign("Jane", "jane@example.com", private)

    assert licensing.verify(key, public_key_b64=wrong_public) is None


@pytest.mark.parametrize("key", ["", "not-a-key", "a.b", "a.b.c", "!.AAAA"])
def test_malformed_license_key_is_invalid(key):
    assert licensing.verify(key) is None
    assert licensing.is_valid(key) is False


def test_malformed_public_key_is_invalid():
    private, _ = licensing.generate_keypair()
    key = licensing.sign("Jane", "jane@example.com", private)

    assert licensing.verify(key, public_key_b64="not/base64!") is None


def test_edition_validation_is_enforced(monkeypatch):
    private, public = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", public)
    key = _signed_payload(private, edition="enterprise")

    assert licensing.verify(key, public_key_b64=public) is None
    assert licensing.is_valid(key) is False
