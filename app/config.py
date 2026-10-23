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
    # SCVMM custom property on the VM holding its retention in days.
    # A whole number above 0 is always used as-is. Blank, missing or anything else: default_retention_days.
    custom_property: str | None = "BackupTier"


@dataclass
class RetierConfig:
    auto_upgrade: bool = True        # retention raised: move VM and restore points to the longer job
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
    compression: str = "Auto"                # copy job storage settings, needed when setting encryption:
    storage_optimization: str = "LocalTarget"   # Auto|None|DedupeFriendly|Optimal|High|Extreme ; LocalTarget|LocalTargetHugeBackup|LANTarget|WANTarget|Automatic


@dataclass
class SafetyConfig:
    reserve_foreign_job_times: bool = True   # keep managed starts off minutes used by VMware/Nutanix/other jobs


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class GfsConfig:
    # Weekly GFS ("Keep certain full backups longer for archival purposes") on every managed backup
    # job and copy job, with a weekly synthetic full on the same day. Days are spread across jobs.
    enabled: bool = True
    weekly_keep_weeks: int = 1
    days: list[str] = field(default_factory=lambda: list(WEEKDAYS))   # days the app may choose from
    copy_jobs: bool = True           # also on backup copy jobs (same day as their source job)


@dataclass
class EncryptionConfig:
    # Backup file encryption on every backup job and copy job the orchestrator manages, using the key
    # in VBR's key vault (Password Manager) whose description matches exactly. No key, no new job.
    enabled: bool = True
    key_description: str = "ITGlue"


@dataclass
class CleanupConfig:
    # VMs that no longer exist anywhere in SCVMM, removed from the orchestrator's own jobs only.
    deleted_vms: str = "approve"     # approve | auto | off
    max_per_run: int = 20            # more than this in one run looks like an inventory problem: blocked


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str | None = None
    port: int = 587
    security: str = "starttls"       # starttls | ssl | none
    username: str | None = None
    password: str | None = None      # SMTP_PASSWORD in .env
    sender: str | None = None
    to: list[str] = field(default_factory=list)
    subject_prefix: str = "[HVBO]"
    app_url: str | None = None       # link back to the UI in emails, e.g. https://hvbo.arc.local
    on_change: bool = True           # jobs created or changed (including failures while applying)
    on_attention: bool = True        # actions waiting for approval, or blocked
    on_failure: bool = True          # a run failed outright (e.g. cannot reach SCVMM or VBR)
    reminder_hours: int = 24         # repeat an unchanged attention/failure alert after this long; 0 = never
    timeout: int = 30


@dataclass
class ScheduleConfig:
    window_start: str = "18:00"
    window_end: str = "00:00"
    stagger_minutes: int = 3
    avoid_unmanaged_job_times: bool = True
    order: str = "first_free"  # first_free | longest_first (used by rebalance; large_first also accepted)


@dataclass
class AppConfig:
    mock: bool = False
    data_dir: str = "/data"
    sync_interval_minutes: int = 60
    auto_apply: bool = False
    coverage_mode: str = "any_hyperv_job"  # any_hyperv_job | managed_only
    default_retention_days: int = 30
    retention_repositories: dict = field(default_factory=dict)   # {days: repository}, e.g. {365: "SOBR-LTR"}
    include_stopped_vms: bool = True
    job_name_template: str = "HVBO {cloud} {tier_label}"
    job_name_max_length: int = 50
    template_job: str | None = None
    default_repository: str = "Default Backup Repository"
    cloud_repositories: dict[str, str] = field(default_factory=dict)
    cloud_tier_repositories: dict[str, dict[str, str]] = field(default_factory=dict)
    exclude_clouds: list[str] = field(default_factory=list)   # cloud names or IDs
    include_clouds: list[str] = field(default_factory=list)   # cloud names or IDs
    rename_jobs_on_cloud_rename: str = "approve"   # approve | auto | off
    tiers: dict[str, TierConfig] = field(default_factory=dict)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    retier: RetierConfig = field(default_factory=RetierConfig)
    backup_copy: BackupCopyConfig = field(default_factory=BackupCopyConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    gfs: GfsConfig = field(default_factory=GfsConfig)
    encryption: EncryptionConfig = field(default_factory=EncryptionConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    vbr: RemoteHost | None = None
    scvmm: RemoteHost | None = None
    scvmm_server_name: str = "localhost"
    source_path: str | None = None

    @property
    def default_tier(self) -> str:
        return self.tier_for_days(self.default_retention_days)

    @staticmethod
    def _for_cloud(mapping: dict, cloud_id: str | None, cloud_name: str | None):
        """Per-cloud settings may be keyed by cloud ID (survives renames) or by name. ID wins."""
        if not mapping:
            return None
        if cloud_id:
            lowered = {str(k).lower(): v for k, v in mapping.items()}
            if str(cloud_id).lower() in lowered:
                return lowered[str(cloud_id).lower()]
        return mapping.get(cloud_name) if cloud_name else None

    def cloud_matches(self, entries: list, cloud_id: str | None, cloud_name: str | None) -> bool:
        wanted = {str(e).lower() for e in entries or []}
        return bool({str(cloud_id or "").lower(), str(cloud_name or "").lower()} & wanted - {""})

    def cloud_in_scope(self, cloud_id: str | None, cloud_name: str | None) -> bool:
        if self.include_clouds and not self.cloud_matches(self.include_clouds, cloud_id, cloud_name):
            return False
        return not self.cloud_matches(self.exclude_clouds, cloud_id, cloud_name)

    def repository_for(self, cloud_id: str | None, cloud_name: str | None, tier: str) -> str:
        days = self.tiers[tier].retention_days if tier in self.tiers else None

        def lookup(mapping: dict | None):
            for k in (tier, days, str(days)):
                if mapping and k is not None and k in mapping:
                    return mapping[k]
            return None

        return (lookup(self._for_cloud(self.cloud_tier_repositories, cloud_id, cloud_name))
                or self._for_cloud(self.cloud_repositories, cloud_id, cloud_name)
                or lookup(self.retention_repositories)
                or self.default_repository)

    def tier_for_days(self, days: int) -> str:
        """Tier key for a retention in days: one key, one job per cloud, per distinct value."""
        key = f"d{int(days)}"
        if key not in self.tiers:
            self.tiers[key] = TierConfig(key=key, label=f"{int(days)}d", retention_days=int(days))
        return key

    def tiers_by_retention(self, keys=None) -> list[str]:
        keys = list(keys if keys is not None else self.tiers)
        return sorted(keys, key=lambda k: (-(self.tiers[k].retention_days if k in self.tiers else 0), k))

    def copy_repository_for(self, cloud_id: str | None, cloud_name: str | None) -> str | None:
        return (self._for_cloud(self.backup_copy.cloud_repositories, cloud_id, cloud_name)
                or self.backup_copy.target_repository)

    def redacted(self) -> dict[str, Any]:
        """Config as a dict with secrets masked, for the settings page."""
        import dataclasses

        d = dataclasses.asdict(self)
        for key in ("vbr", "scvmm"):
            if d.get(key) and d[key].get("password"):
                d[key]["password"] = "********"
        if d.get("email", {}).get("password"):
            d["email"]["password"] = "********"
        return d


# Job markers written before retention became a plain number of days.
LEGACY_TIER_DAYS = {"large": 90}


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


def _email(raw: dict) -> EmailConfig:
    e = EmailConfig(**{k: v for k, v in raw.items() if k in EmailConfig.__dataclass_fields__ and k != "password"})
    if isinstance(e.to, str):
        e.to = [x.strip() for x in e.to.replace(";", ",").split(",") if x.strip()]
    e.username = _secret("SMTP_USERNAME", e.username)
    e.password = _secret("SMTP_PASSWORD")
    return e


def load_config(path: str | None = None) -> AppConfig:
    path = path or os.environ.get("HVBO_CONFIG", "/config/config.yaml")
    raw: dict[str, Any] = {}
    if Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

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
        default_retention_days=int(raw.get("default_retention_days", 30)),
        retention_repositories=raw.get("retention_repositories") or {},
        include_stopped_vms=bool(raw.get("include_stopped_vms", True)),
        job_name_template=raw.get("job_name_template", "HVBO {cloud} {tier_label}"),
        job_name_max_length=int(raw.get("job_name_max_length", 50)),
        template_job=raw.get("template_job"),
        default_repository=raw.get("default_repository", "Default Backup Repository"),
        cloud_repositories=raw.get("cloud_repositories") or {},
        cloud_tier_repositories=raw.get("cloud_tier_repositories") or {},
        exclude_clouds=raw.get("exclude_clouds") or [],
        include_clouds=raw.get("include_clouds") or [],
        classification=cls,
        schedule=sch,
        retier=ret,
        backup_copy=section(BackupCopyConfig, "backup_copy"),
        safety=section(SafetyConfig, "safety"),
        cleanup=section(CleanupConfig, "cleanup"),
        gfs=section(GfsConfig, "gfs"),
        encryption=section(EncryptionConfig, "encryption"),
        email=_email(raw.get("email") or {}),
        rename_jobs_on_cloud_rename=str(raw.get("rename_jobs_on_cloud_rename", "approve")).lower(),
        vbr=_remote(raw.get("vbr"), "VBR"),
        scvmm=_remote(raw.get("scvmm"), "SCVMM"),
        scvmm_server_name=(raw.get("scvmm") or {}).get("vmm_server_name", "localhost"),
        source_path=path,
    )
    days = [d.strip().capitalize() for d in cfg.gfs.days or WEEKDAYS]
    bad = [d for d in days if d not in WEEKDAYS]
    if bad:
        raise ValueError(f"gfs.days has unknown day(s): {bad}")
    cfg.gfs.days = [d for d in WEEKDAYS if d in days]
    if cfg.gfs.weekly_keep_weeks < 1:
        raise ValueError("gfs.weekly_keep_weeks must be 1 or more")
    if cfg.encryption.enabled and not (cfg.encryption.key_description or "").strip():
        raise ValueError("encryption.key_description must name the key in VBR's key vault")
    if cfg.default_retention_days <= 0:
        raise ValueError("default_retention_days must be a whole number above 0")
    cfg.tier_for_days(cfg.default_retention_days)
    return cfg
