"""Isolation tests: nothing outside the orchestrator's own jobs may ever be changed."""
import re
from pathlib import Path

import pytest

from app.backend import MockBackend
from app.config import AppConfig, BackupCopyConfig
from app.engine import Engine
from app.db import Store
from app.safety import SafetyError, validate_ops

PS = Path(__file__).parent.parent / "app" / "ps"

PLAN = {"owned": {"backup_job_ids": ["mine"], "copy_job_ids": ["mycopy"],
                  "foreign_names": ["vmw-prod-sql", "ntnx-ahv-nightly", "legacy acme daily"]}}


@pytest.mark.parametrize("op", [
    {"op": "set_schedule", "job_id": "vmware-job", "start_time": "18:00"},
    {"op": "ensure_job", "job_id": "vmware-job", "job_name": "x", "vms": []},
    {"op": "ensure_job", "job_name": "VMW-Prod-SQL", "vms": []},
    {"op": "retier_vm", "from_job_id": "legacy", "job_id": "mine", "job_name": "x", "vms": []},
    {"op": "retier_vm", "from_job_id": "mine", "job_id": "nutanix", "job_name": "x", "vms": []},
    {"op": "ensure_copy", "source_job_id": "vmware-job", "source_job_name": "x", "copy_name": "new"},
    {"op": "ensure_copy", "source_job_id": "mine", "source_job_name": "x", "copy_name": "NTNX-AHV-Nightly"},
    {"op": "remove_vms", "job_id": "mine"},
    {"op": "delete_job", "job_id": "mine"},
])
def test_validate_refuses_anything_not_ours(op):
    with pytest.raises(SafetyError):
        validate_ops([{"id": "1", **op}], PLAN)


def test_validate_allows_our_jobs_and_new_names():
    validate_ops([
        {"id": "1", "op": "set_schedule", "job_id": "mine", "start_time": "18:00"},
        {"id": "2", "op": "ensure_job", "job_name": "HVBO New 30d", "vms": []},
        {"id": "3", "op": "retier_vm", "from_job_id": "mine", "job_name": "HVBO New 90d", "vms": []},
        {"id": "4", "op": "ensure_copy", "source_job_name": "HVBO New 30d", "copy_name": "HVBO New 30d Copy"},
    ], PLAN)


def test_one_bad_op_stops_the_whole_batch():
    with pytest.raises(SafetyError):
        validate_ops([{"id": "1", "op": "set_schedule", "job_id": "mine", "start_time": "18:00"},
                      {"id": "2", "op": "set_schedule", "job_id": "foreign", "start_time": "18:03"}], PLAN)


MUTATING = re.compile(r"\b(Add|Set|Remove|Enable|Disable|Start|Stop|Move|New|Copy|Update|Sync|Reset|Import|Export|Invoke)-VBR\w+", re.I)
ALLOWED = {"add-vbrhvbackupjob", "add-vbrhvjobobject", "remove-vbrjobobject", "set-vbrjoboptions",
           "set-vbrjobvssoptions", "set-vbrjobschedule", "enable-vbrjobschedule", "add-vbrbackupcopyjob",
           "move-vbrbackup"}


def test_powershell_uses_only_allowlisted_change_cmdlets():
    used = {m.group(0).lower() for f in PS.glob("*.ps1") for m in MUTATING.finditer(f.read_text())}
    assert used <= ALLOWED, f"Unexpected change cmdlets: {used - ALLOWED}"


def test_read_only_scripts_change_nothing():
    for name in ("vbr_inventory.ps1", "vbr_probe.ps1", "scvmm_inventory.ps1", "scvmm_probe.ps1"):
        assert not MUTATING.search((PS / name).read_text()), name


def test_every_apply_operation_asserts_ownership_before_changing():
    text = (PS / "vbr_apply.ps1").read_text()
    blocks = re.split(r"\n\s{12}'(\w+)' \{", text)[1:]
    ops = dict(zip(blocks[0::2], blocks[1::2]))
    assert set(ops) >= {"ensure_job", "set_schedule", "retier_vm", "ensure_copy"}
    for name, body in ops.items():
        first_change = MUTATING.search(body)
        first_assert = body.find("Assert-HvboOwned")
        if first_change:
            assert 0 <= first_assert < first_change.start(), f"{name} changes something before Assert-HvboOwned"
    # Helper functions that change jobs assert too.
    for fn in ("Set-HvboRetention", "Set-HvboSchedule"):
        body = text[text.index(f"function {fn}"):]
        body = body[: body.index("\n}\n")]
        assert body.find("Assert-HvboOwned") < MUTATING.search(body).start()


def test_end_to_end_mock_never_touches_vmware_or_nutanix(tmp_path):
    c = AppConfig(default_repository="SOBR-Tenants", mock=True, data_dir=str(tmp_path))
    c.backup_copy = BackupCopyConfig(enabled=True, target_repository="SOBR-Copy-7d")
    eng = Engine(c, Store(str(tmp_path)))
    import copy, time
    before = copy.deepcopy(eng.backend.state["foreign"])
    legacy_before = copy.deepcopy([j for j in eng.backend.state["jobs"] if "[hvbo" not in j["description"]])
    for _ in range(6):
        rid = eng.start("test", "apply")
        while eng.busy:
            time.sleep(0.02)
        run = eng.store.run(rid)
        assert run["status"] in ("applied", "no_changes", "partial"), run.get("error")
    assert eng.backend.state["foreign"] == before
    assert [j for j in eng.backend.state["jobs"] if "[hvbo" not in j["description"]] == legacy_before
    assert eng.backend.state["copy_jobs"], "copy jobs were created"
    assert all(cj["retention_days"] == 7 for cj in eng.backend.state["copy_jobs"])


def test_mock_refuses_foreign_job_even_if_python_guard_bypassed(tmp_path):
    c = AppConfig(mock=True, data_dir=str(tmp_path))
    b = MockBackend(c)
    vmw = b.state["foreign"][0]
    r = b.apply([{"id": "x", "op": "set_schedule", "job_id": vmw["id"], "start_time": "18:00"}])[0]
    assert not r["ok"] and "SAFETY" in r["messages"][0]
    assert b.state["foreign"][0]["start_time"] == vmw["start_time"]
