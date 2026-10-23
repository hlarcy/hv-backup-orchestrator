"""PowerShell remoting (PSRP over WinRM) from the Linux container to Windows hosts."""
from __future__ import annotations

import json
import re
import logging
from pathlib import Path
from typing import Any

from .config import RemoteHost

log = logging.getLogger("hvbo.remote")
PS_DIR = Path(__file__).parent / "ps"


class RemoteError(RuntimeError):
    pass


def load_script(name: str, with_vbr_common: bool = False) -> str:
    body = (PS_DIR / name).read_text(encoding="utf-8")
    if not with_vbr_common:
        return body
    # The param() block must stay first, so splice the helpers in after it.
    first, _, rest = body.partition("\n")
    common = (PS_DIR / "vbr_common.ps1").read_text(encoding="utf-8")
    return f"{first}\n{common}\n{rest}"


def pin_certificate(wsman, thumbprint: str) -> None:
    """Make the PSRP transport accept exactly one certificate, identified by thumbprint.

    urllib3's assert_fingerprint checks the certificate the server presents during the TLS
    handshake against the pin and aborts the connection on any mismatch, before credentials
    are sent. CA chain and hostname checks are not used, which is what makes self-signed
    WinRM certificates and connecting by IP address work.
    """
    transport = wsman.transport
    session = transport._build_session()
    session.verify = False
    session.get_adapter("https://").poolmanager.connection_pool_kw["assert_fingerprint"] = thumbprint
    transport.session = session


def presented_thumbprint(host: str, port: int) -> str | None:
    """SHA-1 thumbprint of the certificate the server presents (diagnostics only)."""
    import hashlib
    import socket
    import ssl

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=10) as sock, ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
        return hashlib.sha1(der).hexdigest() if der else None
    except Exception:  # noqa: BLE001
        return None


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

PS_URI_PREFIX = "http://schemas.microsoft.com/powershell/"


def endpoint_name(configuration_name: str | None) -> str:
    """pypsrp builds the resource URI itself, so pass only the endpoint name
    (e.g. Microsoft.PowerShell or PowerShell.7). A full URI in config is accepted too."""
    name = (configuration_name or "Microsoft.PowerShell").strip()
    while name.lower().startswith(PS_URI_PREFIX):
        name = name[len(PS_URI_PREFIX):]
    return name


def effective_endpoint(target: RemoteHost) -> str:
    """The bridge launches pwsh.exe itself, so it must run in Windows PowerShell 5.1.
    A PowerShell 7 endpoint left in config would defeat it, so it is overridden."""
    name = endpoint_name(target.configuration_name)
    if target.pwsh_bridge and name.lower().startswith("powershell."):
        log.warning("%s: pwsh_bridge is on, using Microsoft.PowerShell instead of %s", target.host, name)
        return "Microsoft.PowerShell"
    return name


def run_script(target: RemoteHost, script: str, payload: dict[str, Any]) -> Any:
    """Run `script` on `target`, passing `payload` as -InputJson, and parse its JSON output."""
    from pypsrp.powershell import PowerShell, RunspacePool
    from pypsrp.wsman import WSMan

    cert_validation: bool | str = target.ca_bundle or target.cert_validation
    if target.cert_thumbprint:
        cert_validation = False  # CA checks are replaced by the thumbprint pin below
    wsman = WSMan(
        target.host,
        username=target.username,
        password=target.password,
        port=target.effective_port,
        ssl=target.ssl,
        auth=target.auth,
        cert_validation=cert_validation,
        operation_timeout=target.operation_timeout,
        read_timeout=target.operation_timeout + 30,
        encryption="auto",
    )
    if target.cert_thumbprint:
        if not target.ssl:
            raise RemoteError(f"{target.host}: cert_thumbprint needs ssl: true.")
        pin_certificate(wsman, target.cert_thumbprint)
    try:
        with RunspacePool(wsman, configuration_name=effective_endpoint(target)) as pool:
            ps = PowerShell(pool)
            if target.pwsh_bridge:
                import base64
                (ps.add_script(load_script("pwsh_bridge.ps1"))
                   .add_parameter("InputJson", json.dumps(payload))
                   .add_parameter("ScriptB64", base64.b64encode(script.encode("utf-8")).decode("ascii"))
                   .add_parameter("Pwsh", target.pwsh_path))
            else:
                ps.add_script(script).add_parameter("InputJson", json.dumps(payload))
            output = ps.invoke()
            errors = [ANSI_RE.sub("", str(e)) for e in ps.streams.error]
    except Exception as exc:  # network, auth, TLS
        hint = ""
        if target.ssl and ("SSL" in str(exc) or "Fingerprint" in str(exc)):
            seen = presented_thumbprint(target.host, target.effective_port)
            if seen:
                hint = f" Server presented certificate thumbprint {seen.upper()}"
                hint += " (does not match cert_thumbprint)." if target.cert_thumbprint else \
                    ". Check this matches the WinRM certificate on the server before pinning it with cert_thumbprint."
        raise RemoteError(f"{target.host} [endpoint {effective_endpoint(target)}"
                          f"{', pwsh bridge' if target.pwsh_bridge else ''}]: {type(exc).__name__}: {exc}{hint}") from exc

    text = "".join(str(o) for o in output if o is not None).strip()
    if not text:
        raise RemoteError(f"{target.host}: no output. Errors: {' | '.join(errors) or 'none'}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RemoteError(f"{target.host}: output was not JSON: {text[:500]}") from exc
    if errors:
        log.warning("%s reported non-terminating errors: %s", target.host, errors)
    return data
