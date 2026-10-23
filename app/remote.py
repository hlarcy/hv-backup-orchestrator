"""PowerShell remoting (PSRP over WinRM) from the Linux container to Windows hosts."""
from __future__ import annotations

import json
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


def run_script(target: RemoteHost, script: str, payload: dict[str, Any]) -> Any:
    """Run `script` on `target`, passing `payload` as -InputJson, and parse its JSON output."""
    from pypsrp.powershell import PowerShell, RunspacePool
    from pypsrp.wsman import WSMan

    cert_validation: bool | str = target.ca_bundle or target.cert_validation
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
    try:
        with RunspacePool(wsman, configuration_name=f"http://schemas.microsoft.com/powershell/{target.configuration_name}") as pool:
            ps = PowerShell(pool)
            ps.add_script(script).add_parameter("InputJson", json.dumps(payload))
            output = ps.invoke()
            errors = [str(e) for e in ps.streams.error]
    except Exception as exc:  # network, auth, TLS
        raise RemoteError(f"{target.host}: {type(exc).__name__}: {exc}") from exc

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
