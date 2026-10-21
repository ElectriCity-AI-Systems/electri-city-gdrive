"""Owner-only ElectriDrive Pro license recovery (no public HTTP endpoint).

Run as a terminal program on the license host, never by pasting a Python heredoc.
The operator must independently match the PayPal transaction, payer and product.
No private key, generated license key or payer address is printed or audited.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from electridrive import licensing  # noqa: E402
from server import license_server as server  # noqa: E402

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_service_env(path: Path) -> None:
    """Load the root-owned systemd EnvironmentFile without executing shell code."""
    accepted = {
        "PAYPAL_ENV", "PAYPAL_RECEIVER_EMAIL", "PAYPAL_CURRENCY", "MIN_AMOUNT",
        "SIGNING_KEY_PATH", "PROCESSED_FILE", "SMTP_HOST", "SMTP_PORT",
        "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_SUBJECT",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if name in accepted:
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            os.environ[name] = value


def _audit_path() -> Path:
    return server._processed_path().with_name("manual_license_resends.log")


def _record_resend(txn_id: str) -> None:
    """Audit only transaction and acceptance timestamp; no payer/secret/key."""
    import fcntl

    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(f"{stamp}\t{txn_id}\tSMTP_ACCEPTED\n")
        fh.flush()
        os.fsync(fh.fileno())


def check_processed(txn_id: str) -> bool:
    """A persisted txn proves prior SMTP acceptance, not inbox delivery."""
    if not server.valid_txn_id(txn_id):
        return False
    return server.already_processed(txn_id)


def resend_verified(txn_id: str, payer_name: str, payer_email: str) -> None:
    """Reissue and SMTP-send ONLY for a manually cross-checked processed payment.

    This function never changes processed.txt and never outputs the license key.
    """
    if not server.valid_txn_id(txn_id) or not _EMAIL_RE.fullmatch(payer_email):
        raise ValueError("Invalid transaction ID or payer email")
    if not payer_name or len(payer_name) > 200:
        raise ValueError("Invalid payer name")
    with server.processing_lock():
        if not server.already_processed(txn_id):
            raise ValueError("Transaction is not processed: STOP")
        key = server.issue_key(payer_name, payer_email)
        verified = licensing.verify(key, licensing.PUBLIC_KEY_B64)
        if verified is None or verified.email != payer_email or verified.name != payer_name:
            raise ValueError("License failed current-key verification: STOP")
        msg = server.build_message(payer_email, payer_name, key)
        server.send_email(msg)  # SMTP acceptance, not guaranteed inbox delivery.
        _record_resend(txn_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Owner-only check/resend for an existing verified ElectriDrive Pro payment"
    )
    parser.add_argument("--env-file", type=Path,
                        default=Path("/etc/electridrive-license.env"))
    parser.add_argument("--resend", action="store_true",
                        help="explicitly offer manual reissue after checking PayPal")
    args = parser.parse_args(argv)
    if not args.env_file.is_file():
        print("STOP: service environment file not found", file=sys.stderr)
        return 2
    load_service_env(args.env_file)
    # Deliberately read user data from the terminal, not command-line args/history.
    try:
        txn_id = input("PayPal transaction ID: ").strip()
        if not check_processed(txn_id):
            print("STOP: transaction not found in processed file.")
            return 2
        print("Transaction previously processed (SMTP accepted at that time).")
        if not args.resend:
            print("Read-only check complete; no email sent.")
            return 0
        print("Verify in PayPal: payment is not refunded, product is ElectriDrive Pro,")
        print("and the payer email/name below match the transaction.")
        name = input("Verified PayPal payer name: ").strip()
        email = input("Verified PayPal payer email: ").strip()
        if input("Type 'RESEND' after matching the payment in PayPal: ").strip() != "RESEND":
            print("Cancelled. No email sent.")
            return 1
        resend_verified(txn_id, name, email)
        print("SMTP accepted a new Pro license email. Inbox delivery NOT confirmed.")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Run this as an interactive terminal command, not a heredoc.")
        return 1
    except Exception as exc:
        # No exception details: SMTP errors can include customer addresses or message data.
        print(f"STOP: resend unsuccessful ({type(exc).__name__}). No key displayed.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
