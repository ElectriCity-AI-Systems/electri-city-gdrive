"""ElectriDrive Pro — automatic license delivery via PayPal IPN.

Flow: PayPal sends an IPN (Instant Payment Notification) for each completed donation
to this server → we verify it back with PayPal → sign an Ed25519 license key (same
scheme as the app) → email it to the donor. No license database needed.

The signing/grant helpers are stdlib-only and unit-tested; Flask and requests are
imported lazily so the logic is testable and the dependency surface stays small.

Deploy: see server/README.md. Configure via env:
  PAYPAL_ENV=live|sandbox          (default live)
  PAYPAL_RECEIVER_EMAIL=you@paypal.com
  SIGNING_KEY_PATH=/path/license_signing_key.pem
  MIN_AMOUNT=0                      (minimum donation to grant; default 0 = any)
  SMTP_HOST, SMTP_PORT=587, SMTP_USER, SMTP_PASS, MAIL_FROM
  PROCESSED_FILE=/var/lib/electridrive/processed.txt   (dedupe txn ids)
"""
from __future__ import annotations

import logging
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

# Make the electridrive package importable when run from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from electridrive import licensing  # noqa: E402

LOGGER = logging.getLogger("electridrive.license_server")

_IPN_URL = {
    "live": "https://ipnpb.paypal.com/cgi-bin/webscr",
    "sandbox": "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr",
}


def ipn_verify_url() -> str:
    return _IPN_URL.get(os.environ.get("PAYPAL_ENV", "live").lower(), _IPN_URL["live"])


def payer_identity(form: dict) -> tuple[str, str]:
    name = " ".join(p for p in (form.get("first_name", ""), form.get("last_name", "")) if p).strip()
    return name or form.get("payer_email", "Supporter"), form.get("payer_email", "")


def should_grant(form: dict, receiver_email: str, min_amount: float = 0.0) -> tuple[bool, str]:
    """Decide whether a (already PayPal-verified) IPN should yield a license."""
    if form.get("payment_status") != "Completed":
        return False, f"payment_status={form.get('payment_status')}"
    receiver = (form.get("receiver_email") or form.get("business") or "").lower()
    if receiver_email and receiver != receiver_email.lower():
        return False, f"receiver mismatch ({receiver})"
    try:
        amount = float(form.get("mc_gross", "0") or 0)
    except ValueError:
        amount = 0.0
    if amount < min_amount:
        return False, f"amount {amount} < min {min_amount}"
    if not form.get("payer_email"):
        return False, "no payer_email"
    return True, "ok"


def issue_key(name: str, email: str, key_path: str | None = None) -> str:
    priv = licensing.load_private_key(key_path or os.environ.get("SIGNING_KEY_PATH"))
    return licensing.sign(name, email, priv)


def _processed_path() -> Path:
    return Path(os.environ.get("PROCESSED_FILE", "/var/lib/electridrive/processed.txt"))


def already_processed(txn_id: str) -> bool:
    p = _processed_path()
    return p.exists() and txn_id in p.read_text(encoding="utf-8").split()


def mark_processed(txn_id: str) -> None:
    p = _processed_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(txn_id + "\n")


def build_message(to_email: str, name: str, key: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = os.environ.get("MAIL_SUBJECT", "Your ElectriDrive Pro license key ⚡")
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = to_email
    msg.set_content(
        f"Hi {name},\n\n"
        "Thank you for supporting ElectriDrive! Here is your Pro license key:\n\n"
        f"{key}\n\n"
        "Activate it in the app: Settings → Access & ElectriDrive Pro → paste the key → "
        "Activate. Then switch Access mode to Full Drive.\n\n"
        "— Electri_C_ity Studios"
    )
    return msg


def send_email(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def verify_with_paypal(raw_body: bytes) -> bool:
    import requests

    resp = requests.post(
        ipn_verify_url(),
        data=b"cmd=_notify-validate&" + raw_body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "ElectriDrive-IPN/1.0"},
        timeout=20,
    )
    return resp.text.strip() == "VERIFIED"


def create_app():
    from flask import Flask, request

    app = Flask(__name__)
    receiver = os.environ.get("PAYPAL_RECEIVER_EMAIL", "")
    min_amount = float(os.environ.get("MIN_AMOUNT", "0") or 0)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/paypal/ipn")
    def ipn():
        raw = request.get_data()
        form = request.form.to_dict()
        # 1) Always 200 to PayPal; do the work but never error back.
        try:
            if not verify_with_paypal(raw):
                LOGGER.warning("IPN failed PayPal verification")
                return ("", 200)
            txn = form.get("txn_id", "")
            if txn and already_processed(txn):
                return ("", 200)
            ok, reason = should_grant(form, receiver, min_amount)
            if not ok:
                LOGGER.info("IPN not granted: %s", reason)
                return ("", 200)
            name, email = payer_identity(form)
            key = issue_key(name, email)
            send_email(build_message(email, name, key))
            if txn:
                mark_processed(txn)
            LOGGER.info("Issued + emailed license to %s", email)
        except Exception:
            LOGGER.exception("IPN handling failed")
        return ("", 200)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
