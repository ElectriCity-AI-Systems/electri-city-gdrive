import ssl
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import license_server as ls

@pytest.mark.parametrize('trust_ca', [False, True], ids=['unknown-ca', 'wrong-hostname'])
def test_real_local_smtp_rejects_untrusted_certificate(monkeypatch, tmp_path, trust_ca):
    import socket
    from datetime import datetime, timedelta, timezone
    from threading import Thread
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'wrong.invalid')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('wrong.invalid')]), critical=False)
            .sign(key, hashes.SHA256()))
    key_path, cert_path = tmp_path / 'smtp-key.pem', tmp_path / 'smtp-cert.pem'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(5)
    errors = []
    after_tls = []
    def serve():
        try:
            with listener.accept()[0] as conn:
                conn.settimeout(5)
                conn.sendall(b'220 test ESMTP\r\n')
                with conn.makefile('rb') as stream:
                    assert stream.readline().lower().startswith(b'ehlo')
                    conn.sendall(b'250-test\r\n250 STARTTLS\r\n')
                    assert stream.readline().strip() == b'STARTTLS'
                    conn.sendall(b'220 start TLS\r\n')
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert_path, key_path)
                try:
                    with context.wrap_socket(conn, server_side=True) as secured:
                        after_tls.append(secured.recv(1024))
                except ssl.SSLError:
                    pass  # expected rejection during the certificate handshake
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', str(listener.getsockname()[1]))
    monkeypatch.setenv('SMTP_USER', 'dummy-only')
    monkeypatch.setenv('SMTP_PASS', 'dummy-only')
    if trust_ca:
        monkeypatch.setenv('SSL_CERT_FILE', str(cert_path))
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            ls.send_email(ls.build_message('recipient@example.test', 'Pat', 'dummy-license'))
    finally:
        thread.join(timeout=6)
        listener.close()
    assert not thread.is_alive()
    assert errors == []
    assert after_tls == []

