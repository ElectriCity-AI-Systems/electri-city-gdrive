"""Safe offline tests for the owner-only ElectriDrive license recovery."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import license_recovery as recovery
from server import license_server as server


@pytest.fixture
def processed(tmp_path, monkeypatch):
    path = tmp_path / "processed.txt"
    monkeypatch.setenv("PROCESSED_FILE", str(path))
    server.mark_processed("VALIDTXN01")
    return path


def test_check_processed_read_only(processed):
    before = processed.read_bytes()
    assert recovery.check_processed("VALIDTXN01")
    assert not recovery.check_processed("MISSINGTXN")
    assert not recovery.check_processed("invalid\\nID")
    assert processed.read_bytes() == before
    assert not processed.with_name("manual_license_resends.log").exists()


def test_resend_acceptance_audited_without_payer_or_license(
    processed, monkeypatch
):
    calls = []
    monkeypatch.setattr(server, "issue_key", lambda n, e: "signed-lic")
    monkeypatch.setattr(
        recovery.licensing, "verify",
        lambda key, pub: SimpleNamespace(name="Test User", email="test@example.com"),
    )
    monkeypatch.setattr(
        server, "send_email", lambda message: calls.append(message)
    )
    recovery.resend_verified("VALIDTXN01", "Test User", "test@example.com")
    assert len(calls) == 1
    assert "test@example.com" == calls[0]["To"]
    audit = processed.with_name("manual_license_resends.log").read_text()
    assert "VALIDTXN01" in audit
    assert "SMTP_ACCEPTED" in audit
    assert "test@example.com" not in audit
    assert "signed-lic" not in audit
    assert processed.read_text().splitlines() == ["VALIDTXN01"]


def test_never_send_without_previously_processed_payment(processed, monkeypatch):
    monkeypatch.setattr(
        server, "issue_key",
        lambda *_: pytest.fail("must not sign unprocessed payment"),
    )
    with pytest.raises(ValueError, match="not processed"):
        recovery.resend_verified("NOTFOUND", "Test User", "test@example.com")


def test_never_send_when_signature_is_wrong(processed, monkeypatch):
    monkeypatch.setattr(server, "issue_key", lambda n, e: "not-valid")
    monkeypatch.setattr(recovery.licensing, "verify", lambda *a: None)
    monkeypatch.setattr(
        server, "send_email", lambda message: pytest.fail("must not send"),
    )
    with pytest.raises(ValueError, match="verification"):
        recovery.resend_verified("VALIDTXN01", "Test User", "test@example.com")
    assert not processed.with_name("manual_license_resends.log").exists()


def test_smtp_failure_does_not_claim_success(processed, monkeypatch):
    monkeypatch.setattr(server, "issue_key", lambda n, e: "signed-lic")
    monkeypatch.setattr(
        recovery.licensing, "verify",
        lambda key, pub: SimpleNamespace(name="Test User", email="test@example.com"),
    )
    def smtp_error(message):
        raise RuntimeError("Rejected test@example.com")
    monkeypatch.setattr(server, "send_email", smtp_error)
    with pytest.raises(RuntimeError):
        recovery.resend_verified("VALIDTXN01", "Test User", "test@example.com")
    assert not processed.with_name("manual_license_resends.log").exists()


def test_bad_address_rejected_before_signing(processed, monkeypatch):
    monkeypatch.setattr(
        server, "issue_key",
        lambda *_: pytest.fail("should reject invalid address before signing"),
    )
    with pytest.raises(ValueError):
        recovery.resend_verified("VALIDTXN01", "Test User", "not-email")


def test_env_file_is_parsed_not_executed(tmp_path, monkeypatch):
    marker = tmp_path / "SHOULD_NOT_EXIST"
    env = tmp_path / "license.env"
    env.write_text(
        f'SMTP_HOST=smtp.example.com\\n'
        f'SMTP_USER="$(touch {marker})"\\n'
        'PAYPAL_CURRENCY=EUR\\n'
        'UNKNOWN_SECRET=ignored\\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("UNKNOWN_SECRET", raising=False)
    recovery.load_service_env(env)
    assert not marker.exists()
    assert "touch" in __import__("os").environ["SMTP_USER"]
    assert "UNKNOWN_SECRET" not in __import__("os").environ
