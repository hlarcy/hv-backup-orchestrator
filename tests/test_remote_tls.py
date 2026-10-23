"""Certificate pinning against a real local TLS server with a self-signed certificate."""
import datetime
import hashlib
import http.server
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.config import RemoteHost, normalise_thumbprint
from app.remote import RemoteError, presented_thumbprint, run_script


class _WsmanStub(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # any reply proves the TLS handshake and pin succeeded
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Basic")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vbr01-selfsigned")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2)).sign(key, hashes.SHA256()))
    (d / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
    srv = http.server.HTTPServer(("127.0.0.1", 0), _WsmanStub)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], hashlib.sha1(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    srv.shutdown()


def _target(port, **kw):
    return RemoteHost(host="127.0.0.1", port=port, ssl=True, username="u", password="p", auth="basic",
                      operation_timeout=5, **kw)


def test_thumbprint_normalisation():
    win = "‎d1 cd 2d e2 86 75 6d 1f 3c 83 59 4f 5f a5 b0 93 0a cb 04 79"
    assert normalise_thumbprint(win) == "d1cd2de286756d1f3c83594f5fa5b0930acb0479"
    assert normalise_thumbprint("D1:CD:2D:E2:86:75:6D:1F:3C:83:59:4F:5F:A5:B0:93:0A:CB:04:79") == \
        "d1cd2de286756d1f3c83594f5fa5b0930acb0479"
    with pytest.raises(ValueError):
        normalise_thumbprint("abc123")


def test_self_signed_rejected_without_pin(tls_server, monkeypatch):
    port, _ = tls_server
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    with pytest.raises(RemoteError) as e:
        run_script(_target(port), "x", {})
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value) or "SSL" in str(e.value)
    assert "presented certificate thumbprint" in str(e.value)


def test_matching_pin_passes_tls(tls_server):
    port, fp = tls_server
    with pytest.raises(RemoteError) as e:
        run_script(_target(port, cert_thumbprint=fp), "x", {})
    # TLS succeeded; it got as far as authentication, which the stub refuses.
    assert "SSL" not in str(e.value) and "Fingerprint" not in str(e.value)


def test_wrong_pin_refused_before_auth(tls_server):
    port, _ = tls_server
    with pytest.raises(RemoteError) as e:
        run_script(_target(port, cert_thumbprint="00" * 20), "x", {})
    assert "Fingerprints did not match" in str(e.value)


def test_presented_thumbprint(tls_server):
    port, fp = tls_server
    assert presented_thumbprint("127.0.0.1", port) == fp


def test_endpoint_name_is_not_double_prefixed():
    from app.remote import endpoint_name
    assert endpoint_name("Microsoft.PowerShell") == "Microsoft.PowerShell"
    assert endpoint_name("http://schemas.microsoft.com/powershell/PowerShell.7") == "PowerShell.7"
    assert endpoint_name(None) == "Microsoft.PowerShell"


def test_resource_uri_sent_to_server():
    """The URI pypsrp actually builds must be the single-prefixed one."""
    from pypsrp.powershell import RunspacePool
    from pypsrp.wsman import WSMan
    from app.remote import endpoint_name
    pool = RunspacePool(WSMan("127.0.0.1", username="u", password="p", ssl=True, auth="ntlm"),
                        configuration_name=endpoint_name("Microsoft.PowerShell"))
    assert pool.resource_uri == "http://schemas.microsoft.com/powershell/Microsoft.PowerShell"
