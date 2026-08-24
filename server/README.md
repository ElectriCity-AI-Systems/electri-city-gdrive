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

## 3. Install and run

```bash
sudo systemd-analyze verify systemd/electridrive-license.service
sudo install -o root -g root -m 0644 systemd/electridrive-license.service \
  /etc/systemd/system/electridrive-license.service
sudo systemctl daemon-reload
sudo systemctl enable --now electridrive-license.service
curl -fsS http://127.0.0.1:8012/health
```

The expected health response is `{"ok":true}`. Never expose port 8012 publicly.

## 4. Point PayPal at it

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

## 5. Test

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
