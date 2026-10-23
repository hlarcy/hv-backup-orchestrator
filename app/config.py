"""Configuration loading.

Non-secret settings live in a YAML file (default /config/config.yaml).
Secrets (passwords) come from environment variables or *_FILE docker secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _secret(name: str, default: str | None = None) -> str | None:
    """Read NAME from env, or the file pointed to by NAME_FILE (docker secrets)."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path and Path(file_path).exists():
        return Path(file_path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


@dataclass
class RemoteHost:
    host: str
    username: str | None = None
    password: str | None = None
    port: int | None = None
    ssl: bool = True
    auth: str = "negotiate"  # negotiate | ntlm | kerberos | credssp | basic
    cert_validation: bool = True
    ca_bundle: str | None = None
    # Pin the server's certificate by thumbprint (SHA-1 as Windows shows it, or SHA-256).
    # When set, the certificate is trusted if and only if its thumbprint matches, so a
    # self-signed certificate works without a CA bundle, and hostname/IP mismatches don't matter.
    cert_thumbprint: str | None = None
    configuration_name: str = "Microsoft.PowerShell"
    # Run scripts in a PowerShell 7 child process launched from the configuration_name endpoint.
    # Needed for VBR 13.x when the PowerShell.7 remoting endpoint is unusable.
    pwsh_bridge: bool = False
    server_name: str | None = None              # VBR only: name for Connect-VBRServer (default: the server's own name)
    accept_local_tls_certificate: bool = True   # VBR only: -ForceAcceptTlsCertificate on that local connection
    pwsh_path: str = r"C:\Program Files\PowerShell\7\pwsh.exe"
    operation_timeout: int = 600

    @property
    def effective_port(self) -> int:
        return self.port or (5986 if self.ssl else 5985)


@dataclass
class TierConfig:
    key: str
    label: str
    retention_days: int
    repository: str | None = None


@dataclass
class ClassificationConfig:
    custom_property: str | None = "BackupTier"
    large_values: list[str] = field(default_factory=lambda: ["LargeData", "Large", "90"])
    large_tags: list[str] = field(default_factory=lambda: ["LargeData"])
    # Every VM is backed up by default. Exclusions are off unless values are listed here.
    exclude_values: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    large_disk_threshold_gb: float | None = None  # off: only the property/tag makes a VM large data
    disk_size_basis: str = "max"  # max (provisioned) | used


@dataclass
class RetierConfig:
    auto_upgrade: bool = True        # 30d -> 90d when the LargeData property/tag appears
    auto_downgrade: bool = False     # 90d -> 30d when it is removed (would shorten history): approval
    auto_cross_cloud: bool = False   # VM moved to another SCVMM cloud: approval
    remap_backups: bool = True       # move existing restore points with the VM (Move-VBRBackup)


@dataclass
class BackupCopyConfig:
    enabled: bool = True
    target_repository: str | None = None     # separate repository or SOBR; required when enabled
    cloud_repositories: dict[str, str] = field(default_factory=dict)
    retention_days: int = 7
    mode: str = "Immediate"                  # Immediate | Periodic
    name_template: str = "{job} Copy"


@dataclass
class SafetyConfig:
    reserve_foreign_job_times: bool = True   # keep managed starts off minutes used by VMware/Nutanix/other jobs


@dataclass
class ScheduleConfig:
    window_start: str = "18:00"
    window_end: str = "00:00"
    stagger_minutes: int = 3
    avoid_unmanaged_job_times: bool = True
    order: str = "first_free"  # first_free | large_first (used by rebalance)


@dataclass
class AppConfig:
    mock: bool = False
    data_dir: str = "/data"
    sync_interval_minutes: int = 60
    auto_apply: bool = False
    coverage_mode: str = "any_hyperv_job"  # any_hyperv_job | managed_only
    default_tier: str = "standard"
    include_stopped_vms: bool = True
    job_name_template: str = "HVBO {cloud} {tier_label}"
    job_name_max_length: int = 50
    template_job: str | None = None
    default_repository: str = "Default Backup Repository"
    cloud_repositories: dict[str, str] = field(default_factory=dict)
    cloud_tier_repositories: dict[str, dict[str, str]] = field(default_factory=dict)
    exclude_clouds: list[str] = field(default_factory=list)
    include_clouds: list[str] = field(default_factory=list)
    tiers: dict[str, TierConfig] = field(default_factory=dict)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    retier: RetierConfig = field(default_factory=RetierConfig)
    backup_copy: BackupCopyConfig = field(default_factory=BackupCopyConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    vbr: RemoteHost | None = None
    scvmm: RemoteHost | None = None
    scvmm_server_name: str = "localhost"
    ui_user: str | None = None
    ui_password: str | None = None
    auth_disabled: bool = False
    source_path: str | None = None

    def repository_for(self, cloud_name: str, tier: str) -> str:
        per_cloud_tier = self.cloud_tier_repositories.get(cloud_name, {})
        if tier in per_cloud_tier:
            return per_cloud_tier[tier]
        if cloud_name in self.cloud_repositories:
            return self.cloud_repositories[cloud_name]
        tier_cfg = self.tiers.get(tier)
        if tier_cfg and tier_cfg.repository:
            return tier_cfg.repository
        return self.default_repository

    def copy_repository_for(self, cloud_name: str) -> str | None:
        return self.backup_copy.cloud_repositories.get(cloud_name) or self.backup_copy.target_repository

    def redacted(self) -> dict[str, Any]:
        """Config as a dict with secrets masked, for the settings page."""
        import dataclasses

        d = dataclasses.asdict(self)
        for key in ("vbr", "scvmm"):
            if d.get(key) and d[key].get("password"):
                d[key]["password"] = "********"
        if d.get("ui_password"):
            d["ui_password"] = "********"
        return d


DEFAULT_TIERS = {
    "large": TierConfig(key="large", label="LargeData 90d", retention_days=90),
    "standard": TierConfig(key="standard", label="Standard 30d", retention_days=30),
}


def normalise_thumbprint(value: str | None) -> str | None:
    """Accept a thumbprint as pasted from Windows: any case, spaces, colons, and the
    invisible left-to-right mark certmgr adds. Returns lowercase hex, or raises."""
    if not value:
        return None
    hexonly = "".join(ch for ch in str(value).lower() if ch in "0123456789abcdef")
    if len(hexonly) not in (40, 64):
        raise ValueError(f"cert_thumbprint must be a 40-character SHA-1 or 64-character SHA-256 hex value, "
                         f"got {len(hexonly)} hex characters from '{value}'")
    return hexonly


def _remote(section: dict | None, prefix: str) -> RemoteHost | None:
    if not section or not section.get("host"):
        return None
    return RemoteHost(
        host=section["host"],
        username=_secret(f"{prefix}_USERNAME", section.get("username")),
        password=_secret(f"{prefix}_PASSWORD"),
        port=section.get("port"),
        ssl=bool(section.get("ssl", True)),
        auth=section.get("auth", "negotiate"),
        cert_validation=bool(section.get("cert_validation", True)),
        ca_bundle=section.get("ca_bundle"),
        cert_thumbprint=normalise_thumbprint(_secret(f"{prefix}_CERT_THUMBPRINT", section.get("cert_thumbprint"))),
        configuration_name=section.get("configuration_name", "Microsoft.PowerShell"),
        operation_timeout=int(section.get("operation_timeout", 600)),
        pwsh_bridge=bool(section.get("pwsh_bridge", False)),
        server_name=section.get("server_name"),
        accept_local_tls_certificate=bool(section.get("accept_local_tls_certificate", True)),
        pwsh_path=section.get("pwsh_path") or r"C:\Program Files\PowerShell\7\pwsh.exe",
    )


def load_config(path: str | None = None) -> AppConfig:
    path = path or os.environ.get("HVBO_CONFIG", "/config/config.yaml")
    raw: dict[str, Any] = {}
    if Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    tiers = dict(DEFAULT_TIERS)
    for key, t in (raw.get("tiers") or {}).items():
        tiers[key] = TierConfig(
            key=key,
            label=t.get("label", key),
            retention_days=int(t["retention_days"]),
            repository=t.get("repository"),
        )

    cls_raw = raw.get("classification") or {}
    cls = ClassificationConfig(**{k: v for k, v in cls_raw.items() if k in ClassificationConfig.__dataclass_fields__})
    sch_raw = raw.get("schedule") or {}
    sch = ScheduleConfig(**{k: v for k, v in sch_raw.items() if k in ScheduleConfig.__dataclass_fields__})

    def section(cls, key):
        raw_s = raw.get(key) or {}
        return cls(**{k: v for k, v in raw_s.items() if k in cls.__dataclass_fields__})

    ret = section(RetierConfig, "retier")
    if "auto_retier" in raw and "retier" not in raw:  # older config files
        ret.auto_upgrade = bool(raw["auto_retier"])

    env_mock = os.environ.get("HVBO_MOCK")
    cfg = AppConfig(
        mock=(env_mock.lower() in ("1", "true", "yes")) if env_mock else bool(raw.get("mock", False)),
        data_dir=os.environ.get("HVBO_DATA_DIR", raw.get("data_dir", "/data")),
        sync_interval_minutes=int(raw.get("sync_interval_minutes", 60)),
        auto_apply=bool(raw.get("auto_apply", False)),
        coverage_mode=raw.get("coverage_mode", "any_hyperv_job"),
        default_tier=raw.get("default_tier", "standard"),
        include_stopped_vms=bool(raw.get("include_stopped_vms", True)),
        job_name_template=raw.get("job_name_template", "HVBO {cloud} {tier_label}"),
        job_name_max_length=int(raw.get("job_name_max_length", 50)),
        template_job=raw.get("template_job"),
        default_repository=raw.get("default_repository", "Default Backup Repository"),
        cloud_repositories=raw.get("cloud_repositories") or {},
        cloud_tier_repositories=raw.get("cloud_tier_repositories") or {},
        exclude_clouds=raw.get("exclude_clouds") or [],
        include_clouds=raw.get("include_clouds") or [],
        tiers=tiers,
        classification=cls,
        schedule=sch,
        retier=ret,
        backup_copy=section(BackupCopyConfig, "backup_copy"),
        safety=section(SafetyConfig, "safety"),
        vbr=_remote(raw.get("vbr"), "VBR"),
        scvmm=_remote(raw.get("scvmm"), "SCVMM"),
        scvmm_server_name=(raw.get("scvmm") or {}).get("vmm_server_name", "localhost"),
        ui_user=_secret("HVBO_UI_USER"),
        ui_password=_secret("HVBO_UI_PASSWORD"),
        auth_disabled=os.environ.get("HVBO_AUTH_DISABLED", "").lower() in ("1", "true", "yes"),
        source_path=path,
    )
    return cfg
