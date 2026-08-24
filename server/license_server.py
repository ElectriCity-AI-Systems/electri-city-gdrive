"""ElectriDrive Pro — automatic license delivery via PayPal IPN.

Flow: PayPal sends an IPN (Instant Payment Notification) for each completed donation
to this server → we verify it back with PayPal → sign an Ed25519 license key (same
scheme as the app) → email it to the donor. No license database needed.

The signing/grant helpers are stdlib-only and unit-tested; Flask and requests are
imported lazily so the logic is testable and the dependency surface stays small.

Deploy: see server/README.md. Configure via env:
  PAYPAL_ENV=live|sandbox          (default live)
  PAYPAL_RECEIVER_EMAIL=you@paypal.com
  PAYPAL_CURRENCY=EUR
  SIGNING_KEY_PATH=/path/license_signing_key.pem
  MIN_AMOUNT=0                      (minimum donation to grant; default 0 = any)
  SMTP_HOST, SMTP_PORT=587, SMTP_USER, SMTP_PASS, MAIL_FROM
  PROCESSED_FILE=/var/lib/electridrive-license/processed.txt   (dedupe txn ids)
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
import sys
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
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

_TXN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def ipn_verify_url() -> str:
    return _IPN_URL.get(os.environ.get("PAYPAL_ENV", "live").lower(), _IPN_URL["live"])


def payer_identity(form: dict) -> tuple[str, str]:
    name = " ".join(p for p in (form.get("first_name", ""), form.get("last_name", "")) if p).strip()
    return name or form.get("payer_email", "Supporter"), form.get("payer_email", "")


def should_grant(
    form: dict,
    receiver_email: str,
    min_amount: float | Decimal = 0.0,
    expected_currency: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a (already PayPal-verified) IPN should yield a license."""
    if form.get("payment_status") != "Completed":
        return False, "payment not completed"
    receiver = (form.get("receiver_email") or form.get("business") or "").lower()
    if receiver_email and receiver != receiver_email.lower():
        return False, "receiver mismatch"
    try:
        amount = Decimal(form.get("mc_gross", "0") or "0")
        minimum = Decimal(str(min_amount))
    except (InvalidOperation, TypeError, ValueError):
        return False, "invalid amount"
    if not amount.is_finite() or amount < minimum:
        return False, "amount below minimum"
    if expected_currency and form.get("mc_currency", "").upper() != expected_currency.upper():
        return False, "currency mismatch"
    if not form.get("payer_email"):
        return False, "no payer_email"
    return True, "ok"


def issue_key(name: str, email: str, key_path: str | None = None) -> str:
    priv = licensing.load_private_key(key_path or os.environ.get("SIGNING_KEY_PATH"))
    return licensing.sign(name, email, priv)


def _processed_path() -> Path:
    return Path(os.environ.get("PROCESSED_FILE", "/var/lib/electridrive-license/processed.txt"))


def valid_txn_id(txn_id: str) -> bool:
    """Only accept transaction IDs that are safe to persist and log."""
    return bool(_TXN_ID_RE.fullmatch(txn_id))


@contextmanager
def processing_lock():
    """Serialize dedupe/send/persist across Gunicorn workers."""
    import fcntl

    processed = _processed_path()
    processed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = processed.with_name(f".{processed.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def already_processed(txn_id: str) -> bool:
    p = _processed_path()
    return p.exists() and txn_id in p.read_text(encoding="utf-8").split()


def mark_processed(txn_id: str) -> None:
    if not valid_txn_id(txn_id):
        raise ValueError("invalid transaction ID")
    p = _processed_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(txn_id + "\n")
        fh.flush()
        os.fsync(fh.fileno())


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
        smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, password)
        refused = smtp.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def verify_with_paypal(raw_body: bytes) -> bool:
    import requests

    resp = requests.post(
        ipn_verify_url(),
        data=b"cmd=_notify-validate&" + raw_body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "ElectriDrive-IPN/1.0"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text.strip() == "VERIFIED"


def create_app():
    from flask import Flask, request

    app = Flask(__name__)
    receiver = os.environ.get("PAYPAL_RECEIVER_EMAIL", "").strip()
    min_amount = Decimal(os.environ.get("MIN_AMOUNT", "0") or "0")
    expected_currency = os.environ.get("PAYPAL_CURRENCY", "EUR").strip().upper()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/paypal/ipn")
    def ipn():
        raw = request.get_data()
        form = request.form.to_dict()

        try:
            verified = verify_with_paypal(raw)
        except Exception as exc:
            # Exception messages from HTTP libraries can contain request data.
            LOGGER.error("PayPal IPN verification failed (%s)", type(exc).__name__)
            return ("", 500)

        if not verified:
            LOGGER.warning("IPN failed PayPal verification")
            return ("", 200)

        txn = form.get("txn_id", "").strip()
        if not valid_txn_id(txn):
            LOGGER.info("Verified IPN not granted: invalid txn_id")
            return ("", 200)

        try:
            # Keep the lock through SMTP acceptance and durable persistence so
            # concurrent deliveries for one txn cannot both pass the dedupe check.
            with processing_lock():
                if already_processed(txn):
                    return ("", 200)
                ok, reason = should_grant(
                    form, receiver, min_amount, expected_currency or None
                )
                if not ok:
                    LOGGER.info("Verified IPN not granted: %s", reason)
                    return ("", 200)
                if not receiver:
                    # A missing merchant identity is configuration failure, not a
                    # reason to grant a verified payment to an arbitrary receiver.
                    LOGGER.error("Verified IPN cannot be processed: receiver not configured")
                    return ("", 500)
                name, email = payer_identity(form)
                key = issue_key(name, email)
                send_email(build_message(email, name, key))
                mark_processed(txn)
        except Exception as exc:
            # Do not log exception text: SMTP errors can include payer addresses.
            LOGGER.error(
                "Verified IPN processing failed for txn_id=%s (%s)",
                txn,
                type(exc).__name__,
            )
            return ("", 500)

        LOGGER.info("Issued and delivered license for txn_id=%s", txn)
        return ("", 200)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
