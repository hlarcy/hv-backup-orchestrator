"""The example config must load and mention every setting the app reads, so it never drifts."""
import dataclasses
from pathlib import Path

import yaml

from app import config as C

EXAMPLE = Path(__file__).parent.parent / "config" / "config.example.yaml"
SECTIONS = {"classification": C.ClassificationConfig, "schedule": C.ScheduleConfig, "retier": C.RetierConfig,
            "backup_copy": C.BackupCopyConfig, "safety": C.SafetyConfig, "cleanup": C.CleanupConfig,
            "email": C.EmailConfig, "gfs": C.GfsConfig, "encryption": C.EncryptionConfig}
NOT_IN_FILE = {"tiers", "source_path", "scvmm_server_name", "vbr", "scvmm", *SECTIONS}


def fields(cls):
    return {f.name for f in dataclasses.fields(cls)}


def test_example_loads(monkeypatch):
    for k in ("HVBO_MOCK", "HVBO_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    c = C.load_config(str(EXAMPLE))
    assert c.encryption.key_description == "ITGlue" and c.gfs.weekly_keep_weeks == 1
    assert c.vbr.pwsh_bridge and not c.scvmm.pwsh_bridge and c.default_retention_days == 30


def test_example_mentions_every_setting():
    raw = yaml.safe_load(EXAMPLE.read_text())
    assert fields(C.AppConfig) - NOT_IN_FILE <= set(raw), fields(C.AppConfig) - NOT_IN_FILE - set(raw)
    assert set(raw) <= fields(C.AppConfig), set(raw) - fields(C.AppConfig)
    for name, cls in SECTIONS.items():
        want = fields(cls) - {"password"}            # secrets live in .env
        assert set(raw[name]) == want, (name, want ^ set(raw[name]))
    for host in ("vbr", "scvmm"):
        want = fields(C.RemoteHost) - {"password"}
        assert set(raw[host]) - {"vmm_server_name"} == want, (host, want ^ (set(raw[host]) - {"vmm_server_name"}))
