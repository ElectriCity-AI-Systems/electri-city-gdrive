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
