# ElectriDrive Pro — automatic license delivery (PayPal IPN)

A tiny web service: PayPal notifies it of each completed donation → it signs an
Ed25519 license key (same scheme the app verifies) → emails it to the donor.

## 1. Prerequisites
- The signing **private key** (`license_signing_key.pem`) — the one created by
  `python -m electridrive.licensing keygen`. Copy it to the server, readable only by the
  service user (`chmod 600`). *Security:* whoever holds it can mint Pro keys; keep the box
  locked down. (You can use a dedicated key just for the server.)
- An SMTP account for sending mail (your mailbox, SendGrid, Mailgun, etc.).
- A host with a public HTTPS URL (Fly.io, Render, a small VPS, …). PayPal must reach it.

## 2. Configure (env)
```bash
export PAYPAL_ENV=live                      # or sandbox while testing
export PAYPAL_RECEIVER_EMAIL=you@paypal.com # must match the donation account
export SIGNING_KEY_PATH=/srv/electridrive/license_signing_key.pem
export MIN_AMOUNT=0                          # set e.g. 1 to require a minimum donation
export SMTP_HOST=smtp.example.com SMTP_PORT=587 SMTP_USER=apikey SMTP_PASS=*** 
export MAIL_FROM="ElectriDrive <noreply@electri-city.studio>"
export PROCESSED_FILE=/var/lib/electridrive/processed.txt
```

## 3. Run
```bash
pip install -r server/requirements.txt
gunicorn -w 2 -b 0.0.0.0:8000 "license_server:create_app()"   # run from the server/ dir
# or: python server/license_server.py   (dev only)
```
Put it behind HTTPS (Caddy/Nginx/your platform). Health check: `GET /health`.

## 4. Point PayPal at it
- PayPal → Account Settings → **Notifications → Instant Payment Notifications** → enable,
  set the IPN URL to `https://your-host/paypal/ipn`.
- (Optional) add `notify_url=https://your-host/paypal/ipn` to the donate button.

## 5. Test
Use **sandbox** first (`PAYPAL_ENV=sandbox`, an SMTP test inbox), make a sandbox donation,
confirm the email arrives and the key activates in the app. Then switch to `live`.

## Notes
- Idempotent: each PayPal `txn_id` is processed once (`PROCESSED_FILE`).
- The service never returns an error to PayPal (always 200) so PayPal won't retry-spam.
- Modern alternative: PayPal REST **Webhooks** (signature-verified). IPN is used here because
  it works directly with hosted *donate* buttons with the least setup.
