"""Local queue administration must not deliver during initialization."""
import pytest
from server import license_retry as retry


def test_initialize_does_not_send(monkeypatch):
    monkeypatch.setattr(retry.os, 'geteuid', lambda: 1000)
    events = []
    from contextlib import contextmanager
    @contextmanager
    def lock():
        events.append('locked')
        yield
    monkeypatch.setattr(retry.server, 'processing_lock', lock)
    monkeypatch.setattr(retry.server, '_initialize_deliveries', lambda: events.append('imported'))
    monkeypatch.setattr(retry.server, 'retry_deliveries', lambda: pytest.fail('initialization sent email'))
    assert retry.main(['--initialize']) == 0
    assert events == ['locked', 'imported']


def test_retry_command_refuses_root(monkeypatch):
    monkeypatch.setattr(retry.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(retry.server, 'retry_deliveries', lambda: pytest.fail('root modified state'))
    assert retry.main([]) == 2


def test_retry_command_does_not_expose_errors(monkeypatch, capsys):
    monkeypatch.setattr(retry.os, 'geteuid', lambda: 1000)
    def fail():
        raise OSError('payer@example.test secret-key')
    monkeypatch.setattr(retry.server, 'retry_deliveries', fail)
    assert retry.main([]) == 2
    out = capsys.readouterr()
    assert 'payer@example.test' not in out.err
    assert 'secret-key' not in out.err
