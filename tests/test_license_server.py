import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import license_server as ls  # noqa: E402

from electridrive import licensing  # noqa: E402


def test_should_grant_completed_ok():
    form = {"payment_status": "Completed", "receiver_email": "me@x.com",
            "mc_gross": "5.00", "payer_email": "a@b.com"}
    ok, _ = ls.should_grant(form, "me@x.com", 0)
    assert ok


def test_should_grant_rejects_pending():
    ok, _ = ls.should_grant({"payment_status": "Pending", "payer_email": "a@b.com"}, "", 0)
    assert not ok


def test_should_grant_rejects_wrong_receiver():
    form = {"payment_status": "Completed", "receiver_email": "other@x.com",
            "payer_email": "a@b.com", "mc_gross": "5"}
    ok, _ = ls.should_grant(form, "me@x.com", 0)
    assert not ok


def test_should_grant_min_amount():
    form = {"payment_status": "Completed", "payer_email": "a@b.com", "mc_gross": "1.00"}
    ok, _ = ls.should_grant(form, "", 3.0)
    assert not ok


def test_should_grant_rejects_wrong_currency():
    form = {"payment_status": "Completed", "receiver_email": "me@x.com",
            "payer_email": "a@b.com", "mc_gross": "5", "mc_currency": "USD"}
    ok, _ = ls.should_grant(form, "me@x.com", 0, "EUR")
    assert not ok


def test_should_grant_rejects_non_finite_amount():
    form = {"payment_status": "Completed", "payer_email": "a@b.com", "mc_gross": "NaN"}
    ok, _ = ls.should_grant(form, "", 0)
    assert not ok


def test_payer_identity():
    name, email = ls.payer_identity(
        {"first_name": "Jane", "last_name": "Doe", "payer_email": "j@x.com"})
    assert name == "Jane Doe"
    assert email == "j@x.com"


def test_issue_key_roundtrip(tmp_path, monkeypatch):
    priv, pub = licensing.generate_keypair()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_B64", pub)
    key_path = licensing.save_private_key(priv, tmp_path / "k.pem")
    key = ls.issue_key("Jane", "j@x.com", str(key_path))
    lic = licensing.verify(key)
    assert lic is not None and lic.name == "Jane"


def _completed_ipn(txn_id="TESTTXN001"):
    return {
        "payment_status": "Completed",
        "receiver_email": "merchant@example.com",
        "payer_email": "payer@example.com",
        "first_name": "Test",
        "last_name": "Payer",
        "mc_gross": "5.00",
        "mc_currency": "EUR",
        "txn_id": txn_id,
    }


def _client(monkeypatch, tmp_path, receiver="merchant@example.com"):
    monkeypatch.setenv("PROCESSED_FILE", str(tmp_path / "processed.txt"))
    monkeypatch.setenv("MIN_AMOUNT", "0")
    monkeypatch.setenv("PAYPAL_CURRENCY", "EUR")
    if receiver is None:
        monkeypatch.delenv("PAYPAL_RECEIVER_EMAIL", raising=False)
    else:
        monkeypatch.setenv("PAYPAL_RECEIVER_EMAIL", receiver)
    monkeypatch.setattr(ls, "verify_with_paypal", lambda raw: True)
    return ls.create_app().test_client()


def test_ipn_success_persists_only_after_email_and_deduplicates(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(ls, "issue_key", lambda name, email: "test-license")
    monkeypatch.setattr(ls, "build_message", lambda email, name, key: object())
    monkeypatch.setattr(ls, "send_email", lambda message: events.append("email"))

    original_mark_processed = ls.mark_processed

    def record_persistence(txn_id):
        events.append("persist")
        original_mark_processed(txn_id)

    monkeypatch.setattr(ls, "mark_processed", record_persistence)

    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 200
    assert events == ["email", "persist"]
    assert ls.already_processed("TESTTXN001")

    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 200
    assert events == ["email", "persist"]


def test_ipn_unverified_and_ungrantable_requests_return_200(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(ls, "issue_key", lambda *args: (_ for _ in ()).throw(
        AssertionError("must not issue")))
    monkeypatch.setattr(ls, "verify_with_paypal", lambda raw: False)
    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 200

    monkeypatch.setattr(ls, "verify_with_paypal", lambda raw: True)
    pending = _completed_ipn("TESTTXN002")
    pending["payment_status"] = "Pending"
    assert client.post("/paypal/ipn", data=pending).status_code == 200


def test_ipn_verification_transport_failure_returns_500(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)

    def fail_verification(raw):
        raise TimeoutError("temporary PayPal failure")

    monkeypatch.setattr(ls, "verify_with_paypal", fail_verification)
    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 500


def test_ipn_signing_failure_returns_500_without_persisting(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)

    def fail_signing(name, email):
        raise OSError("temporary key read failure")

    monkeypatch.setattr(ls, "issue_key", fail_signing)
    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 500
    assert not ls.already_processed("TESTTXN001")


def test_ipn_smtp_failure_returns_500_without_persisting_or_logging_email(
        tmp_path, monkeypatch, caplog):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(ls, "issue_key", lambda name, email: "test-license")

    def fail_email(message):
        raise RuntimeError("rejected payer@example.com")

    monkeypatch.setattr(ls, "send_email", fail_email)
    caplog.set_level(logging.ERROR, logger="electridrive.license_server")

    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 500
    assert not ls.already_processed("TESTTXN001")
    assert "payer@example.com" not in caplog.text
    assert "test-license" not in caplog.text


def test_ipn_persistence_failure_returns_500_after_email(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(ls, "issue_key", lambda name, email: "test-license")
    monkeypatch.setattr(ls, "send_email", lambda message: events.append("email"))

    def fail_persistence(txn_id):
        events.append("persist")
        raise OSError("temporary disk failure")

    monkeypatch.setattr(ls, "mark_processed", fail_persistence)
    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 500
    assert events == ["email", "persist"]
    assert not ls.already_processed("TESTTXN001")


def test_ipn_missing_receiver_configuration_returns_500_without_email(
        tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path, receiver=None)
    monkeypatch.setattr(ls, "issue_key", lambda *args: (_ for _ in ()).throw(
        AssertionError("must not issue")))
    assert client.post("/paypal/ipn", data=_completed_ipn()).status_code == 500


def test_ipn_missing_or_unsafe_transaction_id_returns_200(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(ls, "issue_key", lambda *args: (_ for _ in ()).throw(
        AssertionError("must not issue")))

    missing = _completed_ipn("")
    unsafe = _completed_ipn("line1\nline2")
    assert client.post("/paypal/ipn", data=missing).status_code == 200
    assert client.post("/paypal/ipn", data=unsafe).status_code == 200
