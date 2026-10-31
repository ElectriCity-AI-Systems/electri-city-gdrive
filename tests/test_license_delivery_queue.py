"""Offline durable receipt tests; no PayPal, SMTP provider or real credentials."""
import concurrent.futures
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import license_server as ls
from server import license_recovery as recovery

REAL_SEND_EMAIL = ls.send_email


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for key, val in {'PAYPAL_RECEIVER_EMAIL': 'merchant@example.test',
                     'PAYPAL_PRODUCT_FIELD': 'item_name', 'PAYPAL_PRODUCT_VALUE': 'ElectriDrive Pro',
                     'PAYPAL_CURRENCY': 'EUR', 'MIN_AMOUNT': '0',
                     'DELIVERY_DB': str(tmp_path / 'delivery.sqlite'),
                     'PROCESSED_FILE': str(tmp_path / 'processed.txt')}.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(ls, 'verify_with_paypal', lambda raw: True)
    monkeypatch.setattr(ls, 'issue_key', lambda name, email: 'signed-key')
    monkeypatch.setattr(ls, 'send_email', lambda message: None)
    return {'txn_id': 'txn-123', 'payment_status': 'Completed',
            'receiver_email': 'merchant@example.test', 'payer_email': 'payer@example.test',
            'mc_gross': '5.00', 'mc_currency': 'EUR', 'first_name': 'Pat',
            'item_name': 'ElectriDrive Pro'}


def rows():
    with sqlite3.connect(ls.delivery_path()) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM deliveries')]


def test_startup_requires_merchant(configured, monkeypatch):
    monkeypatch.setenv('PAYPAL_RECEIVER_EMAIL', '   ')
    with pytest.raises(ValueError, match='PAYPAL_RECEIVER_EMAIL'):
        ls.create_app()


def test_should_grant_requires_merchant(configured):
    assert not ls.should_grant(dict(configured, receiver_email=' '), ' ')[0]
    assert not ls.should_grant(dict(configured, receiver_email=''), '')[0]


def test_retry_survives_restart_and_reuses_key_and_message_id(configured, monkeypatch):
    attempts = []
    def fail(message):
        attempts.append(message)
        raise RuntimeError('rejected payer@example.test signed-key')
    monkeypatch.setattr(ls, 'send_email', fail)
    app = ls.create_app()
    assert app.test_client().post('/paypal/ipn', data=configured).status_code == 500
    assert rows()[0]['status'] == 'retry'
    assert rows()[0]['attempts'] == 1
    assert rows()[0]['last_error'] == 'RuntimeError'
    assert not ls.already_processed('txn-123')
    monkeypatch.setattr(ls, 'issue_key', lambda *args: pytest.fail('must reuse saved key'))
    monkeypatch.setattr(ls, 'send_email', attempts.append)
    ls.create_app()
    assert ls.retry_deliveries() == 1
    assert len(attempts) == 2
    assert attempts[0]['Message-ID'] == attempts[1]['Message-ID']
    assert attempts[0].get_content() == attempts[1].get_content()
    assert len(attempts[1].get_all('Message-ID')) == 1
    assert rows()[0]['status'] == 'sent'
    assert ls.already_processed('txn-123')
    assert ls._processed_path().read_text().splitlines() == ['txn-123']


def test_immutable_verified_receipt(configured):
    ls.enqueue_delivery(configured)
    modified = dict(configured, payer_email='attacker@example.test', first_name='Other')
    ls.enqueue_delivery(modified)
    assert rows()[0]['email'] == 'payer@example.test'
    assert rows()[0]['name'] == 'Pat'
    assert len(rows()) == 1
    assert not ls.already_processed('txn-123')


def test_legacy_import_repeated_and_concurrent_never_sends(configured, monkeypatch):
    ls._processed_path().write_text('txn-123\nold-paid\ninvalid/id\n')
    before = ls._processed_path().read_bytes()
    sent = []
    monkeypatch.setattr(ls, 'send_email', sent.append)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: ls.enqueue_delivery(configured), range(8)))
    assert {r['txn_id'] for r in rows()} == {'txn-123', 'old-paid'}
    assert {r['status'] for r in rows()} == {'sent'}
    assert not ls.deliver_transaction('txn-123')
    assert ls.retry_deliveries() == 0
    assert ls.create_app().test_client().post('/paypal/ipn', data=configured).status_code == 200
    assert not sent
    assert ls._processed_path().read_bytes() == before


def test_parallel_ipns_and_retry_send_once(configured, monkeypatch):
    sent = []
    monkeypatch.setattr(ls, 'send_email', sent.append)
    app = ls.create_app()
    def work(n):
        if n % 2:
            return ls.retry_deliveries()
        return app.test_client().post('/paypal/ipn', data=configured).status_code
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        list(pool.map(work, range(12)))
    assert len(sent) == 1
    assert rows()[0]['attempts'] == 1


def test_signing_failure_is_durable(configured, monkeypatch):
    def fail(*args):
        raise OSError('private path')
    monkeypatch.setattr(ls, 'issue_key', fail)
    assert ls.create_app().test_client().post('/paypal/ipn', data=configured).status_code == 500
    assert rows()[0]['status'] == 'retry'
    assert not rows()[0]['license_key']
    monkeypatch.setattr(ls, 'issue_key', lambda *args: 'fixed-key')
    assert ls.retry_deliveries() == 1


def test_failed_receipt_never_sends(configured, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(ls, 'enqueue_delivery', fail)
    monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('receipt not durable'))
    assert ls.create_app().test_client().post('/paypal/ipn', data=configured).status_code >= 500


def test_failed_legacy_import_never_sends(configured, monkeypatch):
    ls._processed_path().mkdir()
    monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('dedupe unavailable'))
    assert ls.create_app().test_client().post('/paypal/ipn', data=configured).status_code >= 500


def test_interrupted_send_stays_quarantined(configured, monkeypatch):
    def interrupt(message):
        raise KeyboardInterrupt('crash')
    monkeypatch.setattr(ls, 'send_email', interrupt)
    with pytest.raises(KeyboardInterrupt):
        ls.create_app().test_client().post('/paypal/ipn', data=configured)
    monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('uncertain send replayed'))
    assert rows()[0]['status'] == 'sending'
    assert ls.retry_deliveries() == 0
    assert not ls.deliver_transaction('txn-123')
    assert not recovery.check_processed('txn-123')


def test_mirror_failure_stays_quarantined(configured, monkeypatch):
    def fail(txn):
        raise OSError('disk full')
    monkeypatch.setattr(ls, 'mark_processed', fail)
    assert ls.create_app().test_client().post('/paypal/ipn', data=configured).status_code == 500
    assert rows()[0]['status'] == 'sending'
    monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('uncertain send replayed'))
    assert ls.retry_deliveries() == 0
    assert not ls.already_processed('txn-123')


def test_read_only_processed_check_does_not_create_db(configured):
    assert not ls.already_processed('missing')
    assert not ls.delivery_path().exists()


def test_storage_files_private(configured):
    ls.enqueue_delivery(configured)
    assert ls.delivery_path().stat().st_mode & 0o077 == 0
    assert ls.delivery_path() == Path(os.environ['DELIVERY_DB'])
    monkey_parent = ls._processed_path().parent
    assert ls.delivery_path().parent == monkey_parent


def test_db_sent_is_available_to_owner_recovery(configured, monkeypatch):
    ls.enqueue_delivery(configured)
    assert ls.deliver_transaction('txn-123')
    ls._processed_path().unlink()
    assert recovery.check_processed('txn-123')
    with sqlite3.connect(ls.delivery_path()) as db:
        db.execute("UPDATE deliveries SET status='retry'")
    assert not recovery.check_processed('txn-123')


@pytest.mark.parametrize('phase', ['connect', 'tls', 'data-disconnect', 'data-oserror',
                                   'data-rejected', 'refused', 'quit'])
def test_smtp_stage_controls_replay(configured, monkeypatch, phase):
    class SMTP:
        def __init__(self, *args, **kwargs):
            if phase == 'connect':
                raise OSError('offline')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            if phase == 'quit':
                raise OSError('QUIT lost')
        def starttls(self, *, context):
            import ssl
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            if phase == 'tls':
                raise ssl.SSLCertVerificationError('untrusted certificate')
        def send_message(self, msg):
            if phase == 'data-disconnect':
                raise ls.smtplib.SMTPServerDisconnected('lost reply with private data')
            if phase == 'data-oserror':
                raise OSError('lost reply with private data')
            if phase == 'data-rejected':
                raise ls.smtplib.SMTPDataError(550, b'rejected payer@example.test')
            if phase == 'refused':
                return {'payer@example.test': (550, b'refused')}
            return {}
    monkeypatch.setenv('SMTP_HOST', 'localhost')
    monkeypatch.delenv('SMTP_USER', raising=False)
    monkeypatch.setattr(ls.smtplib, 'SMTP', SMTP)
    # Use the real SMTP boundary while keeping the offline synthetic provider.
    monkeypatch.setattr(ls, 'send_email', REAL_SEND_EMAIL)
    response = ls.create_app().test_client().post('/paypal/ipn', data=configured)
    if phase == 'quit':
        assert response.status_code == 200
        assert rows()[0]['status'] == 'sent'
    else:
        assert response.status_code == 500
        expected = 'sending' if phase.startswith('data-') and phase != 'data-rejected' else 'retry'
        assert rows()[0]['status'] == expected
    if rows()[0]['status'] in ('sending', 'sent'):
        monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('must not replay'))
        assert ls.retry_deliveries() == 0


def test_final_db_commit_failure_never_replays(configured, monkeypatch):
    from server import delivery_store
    original = delivery_store.set_status
    def fail(path, txn, status, error=None):
        if status == 'sent':
            raise OSError('commit failed')
        return original(path, txn, status, error)
    monkeypatch.setattr(delivery_store, 'set_status', fail)
    assert ls.create_app().test_client().post('/paypal/ipn', data=configured).status_code == 500
    assert rows()[0]['status'] == 'sending'
    assert ls.already_processed('txn-123')
    assert recovery.check_processed('txn-123')
    monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('legacy mirror must suppress replay'))
    assert ls.retry_deliveries() == 0
    assert not ls.deliver_transaction('txn-123')


def test_queued_and_uncertain_payments_cannot_be_owner_resent(configured, monkeypatch):
    ls.enqueue_delivery(configured)
    monkeypatch.setattr(recovery.server, 'issue_key', lambda *args: pytest.fail('unaccepted receipt'))
    for state in ('pending', 'retry', 'sending'):
        with sqlite3.connect(ls.delivery_path()) as db:
            db.execute('UPDATE deliveries SET status=?', (state,))
        with pytest.raises(ValueError, match='not processed'):
            recovery.resend_verified('txn-123', 'Pat', 'payer@example.test')


def test_delivery_path_defaults_to_legacy_sibling(configured, monkeypatch):
    monkeypatch.delenv('DELIVERY_DB')
    assert ls.delivery_path() == ls._processed_path().with_name('deliveries.sqlite')
    from server import license_server as other_import
    assert other_import.delivery_path() == ls.delivery_path()


def test_recovery_env_accepts_delivery_path(configured, tmp_path):
    path = tmp_path / 'service.env'
    path.write_text('DELIVERY_DB="/tmp/owner-only.sqlite"\n')
    recovery.load_service_env(path)
    assert os.environ['DELIVERY_DB'] == '/tmp/owner-only.sqlite'


def test_db_sent_owner_resend_preserves_legacy_and_private_audit(configured, monkeypatch):
    from types import SimpleNamespace
    ls.enqueue_delivery(configured)
    assert ls.deliver_transaction('txn-123')
    ls._processed_path().unlink()
    sent = []
    monkeypatch.setattr(recovery.server, 'issue_key', lambda *args: 'owner-signed-key')
    monkeypatch.setattr(recovery.licensing, 'verify', lambda *args: SimpleNamespace(
        name='Pat', email='payer@example.test'))
    monkeypatch.setattr(recovery.server, 'send_email', sent.append)
    recovery.resend_verified('txn-123', 'Pat', 'payer@example.test')
    assert len(sent) == 1
    assert not ls._processed_path().exists()
    audit = ls._processed_path().with_name('manual_license_resends.log')
    assert audit.stat().st_mode & 0o077 == 0
    assert 'SMTP_ACCEPTED' in audit.read_text()
    assert 'payer@example.test' not in audit.read_text()
    assert 'owner-signed-key' not in audit.read_text()
    assert rows()[0]['status'] == 'sent'


def test_startup_and_health_do_not_send_or_initialize(configured, monkeypatch):
    monkeypatch.setattr(ls, 'send_email', lambda *args: pytest.fail('health delivered mail'))
    assert ls.create_app().test_client().get('/health').json == {'ok': True}
    assert not ls.delivery_path().exists()
