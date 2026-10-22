# ElectriDrive Pro — automatic license delivery (PayPal IPN)

A tiny web service: PayPal notifies it of each completed donation → it signs an
Ed25519 license key (same scheme the app verifies) → emails it to the donor.

## 1. Prerequisites

- The signing **private key** (`license_signing_key.pem`) — the one created by
  `python -m electridrive.licensing keygen`. Copy it to the server, readable only by the
  service user (`chmod 600`). *Security:* whoever holds it can mint Pro keys; keep the box
  locked down. Never generate a replacement during deployment.
- An SMTP account for sending mail (your mailbox, SendGrid, Mailgun, etc.).
- A host with a public HTTPS URL (Fly.io, Render, a small VPS, …). PayPal must reach it.

## 2. Production configuration

Install the repository-managed unit from `systemd/electridrive-license.service`. It expects
`/etc/electridrive-license.env`, owned by `root:electridrive-license` with mode `0640`:

```ini
PAYPAL_ENV=live
PAYPAL_RECEIVER_EMAIL=<PayPal merchant address>
PAYPAL_CURRENCY=EUR
# REQUIRED before updating the running service: match actual VERIFIED ElectriDrive IPN.
# See section "Product binding" below for validation.
PAYPAL_PRODUCT_FIELD=<item_number|item_name|custom>
PAYPAL_PRODUCT_VALUE=<exact verified ElectriDrive Pro value>
SIGNING_KEY_PATH=/opt/electridrive-license/secrets/license_signing_key.pem
MIN_AMOUNT=0
SMTP_HOST=smtp.ionos.de
SMTP_PORT=587
SMTP_USER=<SMTP login>
SMTP_PASS=<SMTP password>
# MAIL_FROM is optional; if omitted, SMTP_USER is used.
PROCESSED_FILE=/var/lib/electridrive-license/processed.txt
```

This is a systemd environment file, not a shell script. Do not `source` it. Keep actual
credentials, customer data, transaction state, and the signing key outside the repository.

For v2.1.0 and later, the server must issue new licenses with the private key matching the
current `PUBLIC_KEY_B64`. Keep that key outside the repository. The application retains the
previous public key only as a verification fallback for licenses issued before the rotation.

The production service uses two Gunicorn workers and binds only to
`127.0.0.1:8012`. Its durable transaction state is
`/var/lib/electridrive-license/processed.txt`.

## 3. Product binding (must be validated before production deploy)

An IPN verified by PayPal proves that PayPal sent payment data. It does **not**
prove the payment was for **ElectriDrive Pro** merely because the merchant,
currency, and payment state match: this merchant also receives unrelated
Studios donations. The license server now **fails closed** until
`PAYPAL_PRODUCT_FIELD` and `PAYPAL_PRODUCT_VALUE` are set.

Choose **one** field (`item_number`, `custom` or `item_name`) from the
**actual raw PayPal IPN notification associated with a confirmed ElectriDrive
Pro payment**. It must be stable for every valid Pro payment and must differ
from normal Studios donations. Prefer `item_number` or a dedicated purpose
code over display text; the hosted button ID in the payment page URL is **not
assumed** to exist in the IPN body. Never configure `txn_id` or payer data as
a product marker. PayPal's standard integration supports pass-through
`item_number` and `custom`, but a hosted donation button might not supply
them for every payment method.

Validate the real IPN metadata in PayPal's IPN history/details without
copying payer names, email addresses, transaction IDs or raw IPN bodies into
tickets or chats. Compare against a known community donation. If no reliable
marker exists, **do not deploy this change yet**: first arrange separate
payment routing or a stronger server-side purchase verification approach.

Example only; NOT a verified value for the existing PayPal hosted button:

```ini
PAYPAL_PRODUCT_FIELD=item_number
PAYPAL_PRODUCT_VALUE=electridrive-pro
```

With a marker configured, missing/different product metadata gets HTTP 200
(verified unrelated payment, no license). An absent/invalid marker configuration
returns HTTP 503 for PayPal-verified IPNs, allowing PayPal to retry once
configuration is fixed. The regular health endpoint remains available.
If you need to correct a real customer payment which was skipped, use the
approved manual reconciliation process after independently verifying the
payment; never replay an unverified forged IPN.

**Threat model:** A plain pass-through product marker reduces accidental
cross-product grants; it is not a cryptographic order-to-product binding.
For stronger protection, issue server-side purchase references and reconcile
payment identity through the PayPal API before granting licenses.

## 4. Install and run

```bash
sudo systemd-analyze verify systemd/electridrive-license.service
sudo install -o root -g root -m 0644 systemd/electridrive-license.service \
  /etc/systemd/system/electridrive-license.service
sudo systemctl daemon-reload
sudo systemctl enable --now electridrive-license.service
curl -fsS http://127.0.0.1:8012/health
```

The expected health response is `{"ok":true}`. Never expose port 8012 publicly.

## 5. Point PayPal at it

- PayPal → Account Settings → **Notifications → Instant Payment Notifications** → enable,
  set the IPN URL to `https://your-host/paypal/ipn`.
- (Optional) add `notify_url=https://your-host/paypal/ipn` to the donate button.

Only add the route to a hostname that is intentionally shared with ElectriDrive. A minimal
nginx route inside that hostname's existing HTTPS `server` block is:

```nginx
location = /paypal/ipn {
    proxy_pass http://127.0.0.1:8012/paypal/ipn;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 6. Test

Use **sandbox** first (`PAYPAL_ENV=sandbox`, an SMTP test inbox), make a sandbox donation,
confirm the email arrives and the key activates in the app. Then switch to `live`.

## Notes

- Idempotent: each PayPal `txn_id` is serialized across workers and persisted once.
- Duplicate, unverified, fake, and ungrantable IPNs return HTTP 200.
- After PayPal verification, transient signing, SMTP, or persistence failures return HTTP 500
  so PayPal can retry. A transaction is persisted only after SMTP accepts its message.
- A verified, otherwise grantable IPN also returns HTTP 500 when
  `PAYPAL_RECEIVER_EMAIL` is missing, preventing licenses for an arbitrary receiver while
  retaining PayPal's retry opportunity.
- Payer addresses and generated license keys are not written to application logs.
- Modern alternative: PayPal REST **Webhooks** (signature-verified). IPN is used here because
  it works directly with hosted *donate* buttons with the least setup.

## 7. Diagnosing missing license emails (operator runbook)

A PayPal IPN HTTP 200 means **only** that the endpoint answered. A transaction
listed in `PROCESSED_FILE` means the application generated a valid signed key,
and the SMTP server accepted the message; it **does not prove inbox delivery**.
Check junk/spam, provider bounce/rejection reports, SPF/DKIM/DMARC alignment,
the recipient address in the original PayPal transaction, and any SMTP provider
logs before concluding that a message was delivered.

The app now logs `SMTP accepted license mail` with a generated Message-ID to the Gunicorn/systemd
journal for **newly processed** transactions. No private key, full payer email,
or signed license is written to that log. Do not paste raw journal output
containing private data into public tickets.

### Controlled owner-only recovery

Use the following commands **on the licensed VPS** after deploying this update.
Never enter a signing key, SMTP password, or full license into chat or CI logs.

```bash
# Normal terminal command — NOT a Python heredoc.
cd /opt/electridrive-license/app

# Read-only: checks the existing processed ledger.
sudo -u electridrive-license /opt/electridrive-license/.venv/bin/python -m server.license_recovery

# Optional: resend for a processed PayPal txn after manual verification.
sudo -u electridrive-license /opt/electridrive-license/.venv/bin/python -m server.license_recovery --resend
```

Run **as the service user**, not root. Running as root could create a
root-owned processed-ledger lock and prevent subsequent Gunicorn payments.
The recovery command actively refuses root execution.

The operator enters the transaction ID interactively, and must cross-check
that PayPal lists this payment as completed, not refunded/reversed, and
**explicitly associated with ElectriDrive Pro**. The payer's full name and
email must match PayPal. The operator types `RESEND` to authorize sending.
The script never prints the generated license, never modifies `processed.txt`,
and records only transaction ID, timestamp, and SMTP acceptance in
`manual_license_resends.log` beside the processed file (mode 0600).

**Security:** this CLI has access to the production signing key and must
remain restricted to the server owner. Do not add a public resend endpoint.
Do not regenerate the private signing key.

### Deployment gate

Do not deploy until the exact marker is confirmed from a genuine ElectriDrive
Pro PayPal notification and the community-button value is also checked.
Before restarting, preserve a backup of the exact current application version,
service environment, private key path and processed ledger. Do not copy the
private key into git, CI artifacts or the deployment bundle. Use the CI-proven
commit, verify `/health`, and retain an immediate rollback to the previously
running release. Never use a forged paid IPN as a production test.
