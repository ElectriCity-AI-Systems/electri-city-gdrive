import pytest
from electridrive.google_api.picker import PickerError

def test_setup_error_closes_callback_listener(monkeypatch):
    import electridrive.google_api.picker as picker
    from google_auth_oauthlib.flow import Flow
    servers = []
    actual_server = picker.HTTPServer
    def track(*args, **kwargs):
        server = actual_server(*args, **kwargs)
        servers.append(server)
        return server
    monkeypatch.setattr(picker, 'HTTPServer', track)
    monkeypatch.setattr('electridrive.google_api.app_client.is_configured', lambda: True)
    monkeypatch.setattr('electridrive.google_api.app_client.is_picker_configured', lambda: True)
    monkeypatch.setattr(Flow, 'from_client_config', lambda *a, **k: (_ for _ in ()).throw(ValueError('bad client')))
    try:
        with pytest.raises(ValueError, match='bad client'):
            picker.pick_from_drive(timeout=0.1)
        assert servers[0].fileno() == -1
    finally:
        for server in servers:
            server.server_close()


def test_partial_loopback_request_cannot_block_picker_timeout(monkeypatch):
    import socket
    from threading import Thread
    from types import SimpleNamespace
    from urllib.parse import urlparse
    import electridrive.google_api.picker as picker
    from google_auth_oauthlib.flow import Flow
    flow = SimpleNamespace(authorization_url=lambda **kw: ('https://example.test', 'expected'))
    monkeypatch.setattr('electridrive.google_api.app_client.is_configured', lambda: True)
    monkeypatch.setattr('electridrive.google_api.app_client.is_picker_configured', lambda: True)
    monkeypatch.setattr(Flow, 'from_client_config', lambda *a, **kw: flow)
    clients = []
    def open_browser(url):
        target = urlparse(flow.redirect_uri)
        client = socket.create_connection(('127.0.0.1', target.port))
        clients.append(client)
        client.sendall(b'GET /?state=expected&code=partial')
    monkeypatch.setattr(picker.webbrowser, 'open', open_browser)
    errors = []
    def run():
        try:
            picker.pick_from_drive(timeout=0.1)
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], PickerError)
        assert 'Timed out' in str(errors[0])
    finally:
        for client in clients:
            client.close()
        thread.join(timeout=2)


@pytest.mark.parametrize('persist', [True, False])
def test_picker_validates_grant_and_optional_persistence(monkeypatch, persist):
    import electridrive.google_api.picker as picker
    from electridrive.config import DEFAULT_SCOPE
    from types import SimpleNamespace
    from urllib.parse import urlparse
    import socket
    from google_auth_oauthlib.flow import Flow
    flow = SimpleNamespace(authorization_url=lambda **kw: ('https://example.test', 'snow☃'),
        credentials=SimpleNamespace(scopes=[DEFAULT_SCOPE]), fetch_token=lambda **kw: None)
    monkeypatch.setattr('electridrive.google_api.app_client.is_configured', lambda: True)
    monkeypatch.setattr('electridrive.google_api.app_client.is_picker_configured', lambda: True)
    monkeypatch.setattr(Flow, 'from_client_config', lambda *a, **kw: flow)
    clients = []
    def open_browser(url):
        target = urlparse(flow.redirect_uri)
        client = socket.create_connection(('127.0.0.1', target.port))
        client.sendall(b'GET /?state=snow%E2%98%83&code=good HTTP/1.0\r\n\r\n')
        clients.append(client)
    monkeypatch.setattr(picker.webbrowser, 'open', open_browser)
    saved = []
    monkeypatch.setattr(picker, 'save_credentials', saved.append)
    try:
        result = picker.pick_from_drive(timeout=0.5, persist=persist)
        assert result.credentials is flow.credentials
        assert len(saved) == int(persist)
    finally:
        for client in clients:
            client.close()


def test_dripping_partial_request_cannot_extend_overall_deadline(monkeypatch):
    import socket
    import time
    from threading import Thread
    from types import SimpleNamespace
    from urllib.parse import urlparse
    from google_auth_oauthlib.flow import Flow
    from electridrive.google_api import picker
    flow = SimpleNamespace(authorization_url=lambda **kw: ('https://example.test', 'expected'))
    monkeypatch.setattr('electridrive.google_api.app_client.is_configured', lambda: True)
    monkeypatch.setattr('electridrive.google_api.app_client.is_picker_configured', lambda: True)
    monkeypatch.setattr(Flow, 'from_client_config', lambda *a, **kw: flow)
    clients = []
    senders = []
    def open_browser(url):
        target = urlparse(flow.redirect_uri)
        client = socket.create_connection(('127.0.0.1', target.port))
        clients.append(client)
        def drip():
            try:
                for _ in range(30):
                    client.sendall(b'G')
                    time.sleep(0.02)
            except OSError:
                pass  # server is expected to close/reset once its deadline fires
        thread = Thread(target=drip, daemon=True)
        senders.append(thread)
        thread.start()
    monkeypatch.setattr(picker.webbrowser, 'open', open_browser)
    errors = []
    def run():
        try:
            picker.pick_from_drive(timeout=0.08)
        except PickerError as exc:
            errors.append(exc)
    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        thread.join(timeout=0.3)
        assert not thread.is_alive()
        assert len(errors) == 1 and 'Timed out' in str(errors[0])
    finally:
        for client in clients:
            client.close()
        thread.join(timeout=2)
        for sender in senders:
            sender.join(timeout=2)
