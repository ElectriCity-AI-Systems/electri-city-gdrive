"""ElectriDrive Pro — automatic license delivery via PayPal IPN.

Flow: PayPal sends an IPN (Instant Payment Notification) for each completed donation
to this server → we verify it back with PayPal → sign an Ed25519 license key (same
scheme as the app) → durably queue and email it to the donor.

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
from email.utils import make_msgid
from pathlib import Path

# Make the electridrive package importable when run from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from electridrive import licensing  # noqa: E402
from server import delivery_store  # noqa: E402

LOGGER = logging.getLogger("electridrive.license_server")

_IPN_URL = {
    "live": "https://ipnpb.paypal.com/cgi-bin/webscr",
    "sandbox": "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr",
}

_TXN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# These are PayPal IPN payment metadata fields, not the hosted_button_id.
# Only select a field observed in a PayPal-verified ElectriDrive Pro IPN.
_PRODUCT_FIELDS = frozenset(("item_number", "item_name", "custom"))


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
    product_field: str | None = None,
    product_value: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a (already PayPal-verified) IPN should yield a license."""
    if not receiver_email.strip():
        return False, "receiver not configured"
    if form.get("payment_status") != "Completed":
        return False, "payment not completed"
    receiver = (form.get("receiver_email") or form.get("business") or "").lower()
    if receiver != receiver_email.strip().lower():
        return False, "receiver mismatch"
    try:
        amount = Decimal(form.get("mc_gross", "0") or "0")
        minimum = Decimal(str(min_amount))
    except (InvalidOperation, TypeError, ValueError):
        return False, "invalid amount"
    if not amount.is_finite() or amount <= 0 or amount < minimum:
        return False, "amount not positive or below minimum"
    if expected_currency and form.get("mc_currency", "").upper() != expected_currency.upper():
        return False, "currency mismatch"
    if not form.get("payer_email"):
        return False, "no payer_email"
    # Only the IPN listener passes enforced product parameters. The direct
    # helper API remains useful for testing basic payment eligibility.
    if product_field is not None or product_value is not None:
        if product_field not in _PRODUCT_FIELDS or not product_value:
            return False, "invalid product configuration"
        if str(form.get(product_field, "")) != product_value:
            return False, "product mismatch"
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
    if not valid_txn_id(txn_id):
        return False
    p = _processed_path()
    if p.exists() and txn_id in p.read_text(encoding="utf-8").split():
        return True
    return delivery_store.accepted(delivery_path(), txn_id)


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
    msg["Message-ID"] = make_msgid()
    msg.set_content(
        f"Hi {name},\n\n"
        "Thank you for supporting ElectriDrive! Here is your Pro license key:\n\n"
        f"{key}\n\n"
        "Activate it in the app: Settings → Access & ElectriDrive Pro → paste the key → "
        "Activate. Then switch Access mode to Full Drive.\n\n"
        "— Electri_C_ity Studios"
    )
    return msg


class SMTPDeliveryUncertain(RuntimeError):
    """SMTP DATA may have been accepted; automatic replay is unsafe."""


def send_email(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    accepted = False
    uncertain = False
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if user:
                smtp.login(user, password)
            try:
                refused = smtp.send_message(msg)
            except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPNotSupportedError):
                # Explicit pre-DATA failure or negative SMTP response is definite.
                raise
            except (smtplib.SMTPServerDisconnected, OSError) as exc:
                uncertain = True
                raise SMTPDeliveryUncertain("SMTP acceptance requires reconciliation") from exc
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            accepted = True
    except Exception:
        # SMTP.__exit__ can replace a DATA exception with its own QUIT error.
        # Once acceptance is uncertain, cleanup cannot make it safe to retry.
        if uncertain:
            raise SMTPDeliveryUncertain("SMTP acceptance requires reconciliation") from None
        # QUIT/disconnect after accepted DATA cannot undo SMTP acceptance.
        if not accepted:
            raise


def delivery_path() -> Path:
    return Path(os.environ.get("DELIVERY_DB", str(_processed_path().with_name("deliveries.sqlite"))))


def _initialize_deliveries() -> None:
    delivery_store.initialize(delivery_path(), _processed_path(), valid_txn_id)


def enqueue_delivery(form: dict) -> None:
    """Persist one immutable verified, eligible receipt before attempting SMTP."""
    txn = form.get("txn_id", "").strip()
    if not valid_txn_id(txn):
        raise ValueError("invalid transaction ID")
    name, email = payer_identity(form)
    with processing_lock():
        _initialize_deliveries()
        delivery_store.enqueue(delivery_path(), txn, name, email, form,
                               os.environ.get("PAYPAL_PRODUCT_FIELD", "").strip())


def _deliver_transaction(txn_id: str) -> bool:
    # Caller holds the same advisory lock as migration and owner-only resend.
    path = delivery_path()
    if already_processed(txn_id):
        return False
    receipt = delivery_store.claim(path, txn_id)
    if receipt is None:
        return False
    try:
        key = receipt["license_key"]
        message_id = receipt["message_id"]
        if not key:
            key = issue_key(receipt["name"], receipt["email"])
            message_id = make_msgid()
            delivery_store.save_key(path, txn_id, key, message_id)
        msg = build_message(receipt["email"], receipt["name"], key)
        if isinstance(msg, EmailMessage):
            if msg.get("Message-ID"):
                msg.replace_header("Message-ID", message_id)
            else:
                msg["Message-ID"] = message_id
        send_email(msg)
    except SMTPDeliveryUncertain:
        # Remain sending, including across restarts and duplicate callbacks.
        raise
    except Exception as exc:
        delivery_store.set_status(path, txn_id, "retry", type(exc).__name__)
        raise
    # Neither failure here may replay an SMTP-accepted message. Keep sending.
    mark_processed(txn_id)
    delivery_store.set_status(path, txn_id, "sent")
    LOGGER.info("SMTP accepted license mail: txn_id=%s message_id=%s; recipient inbox unconfirmed",
                txn_id, message_id)
    return True


def deliver_transaction(txn_id: str) -> bool:
    if not valid_txn_id(txn_id):
        raise ValueError("invalid transaction ID")
    with processing_lock():
        _initialize_deliveries()
        return _deliver_transaction(txn_id)


def retry_deliveries() -> int:
    """Retry definite failures only; never reclaim uncertain sending attempts."""
    delivered = 0
    with processing_lock():
        _initialize_deliveries()
        for txn in delivery_store.pending(delivery_path()):
            try:
                delivered += _deliver_transaction(txn)
            except Exception as exc:
                LOGGER.error("License retry failed: txn_id=%s (%s)", txn, type(exc).__name__)
    return delivered


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
    # Gunicorn does not automatically enable our module logger at INFO level.
    # Flask routes its application logger to the Gunicorn error stream.
    app.logger.setLevel(logging.INFO)
    receiver = os.environ.get("PAYPAL_RECEIVER_EMAIL", "").strip()
    if not receiver:
        raise ValueError("PAYPAL_RECEIVER_EMAIL is required")
    min_amount = Decimal(os.environ.get("MIN_AMOUNT", "0") or "0")
    expected_currency = os.environ.get("PAYPAL_CURRENCY", "EUR").strip().upper()
    product_field = os.environ.get("PAYPAL_PRODUCT_FIELD", "").strip()
    product_value = os.environ.get("PAYPAL_PRODUCT_VALUE", "").strip()

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
        # Fail closed without a verified product binding: EUR payments for
        # unrelated Studios donations must never mint ElectriDrive licenses.
        # 503 permits PayPal to retry once configuration has been corrected.
        if product_field not in _PRODUCT_FIELDS or not product_value:
            app.logger.error("License IPN blocked: missing or invalid product binding")
            return ("", 503)
        if not valid_txn_id(txn):
            LOGGER.info("Verified IPN not granted: invalid txn_id")
            return ("", 200)

        try:
            # Check eligibility on every callback, but never replace a saved receipt.
            ok, reason = should_grant(
                form, receiver, min_amount, expected_currency or None,
                product_field, product_value
            )
            if not ok:
                app.logger.info("Verified IPN declined: txn_id=%s reason=%s", txn, reason)
                return ("", 200)
            enqueue_delivery(form)
            deliver_transaction(txn)
        except Exception as exc:
            # Do not log exception text: SMTP errors can include payer addresses.
            LOGGER.error(
                "Verified IPN processing failed for txn_id=%s (%s)",
                txn,
                type(exc).__name__,
            )
            return ("", 500)

        return ("", 200)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
