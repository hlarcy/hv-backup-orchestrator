"""Isolation guard, checked in Python before anything is sent to VBR.

The VBR server also runs VMware and Nutanix protection. The rule is absolute: the
orchestrator may only modify jobs that carry its own description marker. Every operation
is checked against the ownership list built at plan time, and the PowerShell side checks
the live job again (Assert-HvboOwned) before any change. Either check failing stops the run.
"""
from __future__ import annotations


class SafetyError(RuntimeError):
    pass


def validate_ops(ops: list[dict], plan: dict) -> None:
    owned = plan.get("owned") or {}
    owned_backup = set(owned.get("backup_job_ids") or [])
    owned_copy = set(owned.get("copy_job_ids") or [])
    foreign = {n.lower() for n in owned.get("foreign_names") or []}
    problems: list[str] = []

    def check_existing(job_id: str | None, what: str, op: dict) -> None:
        if job_id and job_id not in owned_backup:
            problems.append(f"{op['op']} {op.get('id')}: {what} {job_id} is not an orchestrator-managed job")

    def check_new_name(name: str | None, op: dict) -> None:
        if not name:
            problems.append(f"{op['op']} {op.get('id')}: no job name")
        elif name.lower() in foreign:
            problems.append(f"{op['op']} {op.get('id')}: name '{name}' belongs to an existing job not managed here")

    for op in ops:
        kind = op.get("op")
        if kind == "ensure_job":
            if op.get("job_id"):
                check_existing(op["job_id"], "job", op)
            else:
                check_new_name(op.get("job_name"), op)
        elif kind == "set_schedule":
            if not op.get("job_id"):
                problems.append(f"set_schedule {op.get('id')}: job id required")
            check_existing(op.get("job_id"), "job", op)
        elif kind == "retier_vm":
            if not op.get("from_job_id"):
                problems.append(f"retier_vm {op.get('id')}: source job id required")
            check_existing(op.get("from_job_id"), "source job", op)
            if op.get("job_id"):
                check_existing(op["job_id"], "target job", op)
            else:
                check_new_name(op.get("job_name"), op)
        elif kind == "ensure_copy":
            if op.get("source_job_id"):
                check_existing(op["source_job_id"], "copy source job", op)
            else:
                check_new_name(op.get("source_job_name"), op)
            if op.get("copy_job_id") and op["copy_job_id"] not in owned_copy:
                problems.append(f"ensure_copy {op.get('id')}: copy job {op['copy_job_id']} is not managed here")
            check_new_name(op.get("copy_name"), op)
        elif kind == "rename_job":
            ids = owned_copy if op.get("job_kind") == "copy" else owned_backup
            if not op.get("job_id") or op["job_id"] not in ids:
                problems.append(f"rename_job {op.get('id')}: job {op.get('job_id')} is not an orchestrator-managed job")
            check_new_name(op.get("new_name"), op)
        elif kind == "set_encryption":
            ids = owned_copy if op.get("job_kind") == "copy" else owned_backup
            if not op.get("job_id") or op["job_id"] not in ids:
                problems.append(f"set_encryption {op.get('id')}: job {op.get('job_id')} is not an orchestrator-managed job")
        elif kind == "set_gfs":
            ids = owned_copy if op.get("job_kind") == "copy" else owned_backup
            if not op.get("job_id") or op["job_id"] not in ids:
                problems.append(f"set_gfs {op.get('id')}: job {op.get('job_id')} is not an orchestrator-managed job")
        elif kind == "remove_deleted_vms":
            if not op.get("job_id"):
                problems.append(f"remove_deleted_vms {op.get('id')}: job id required")
            check_existing(op.get("job_id"), "job", op)
            if any(not v.get("vm_guid") for v in op.get("vms") or []):
                problems.append(f"remove_deleted_vms {op.get('id')}: every VM must be identified by VM ID")
        else:
            problems.append(f"Unknown operation '{kind}'")

    if problems:
        raise SafetyError("Safety check refused the batch; nothing was sent to VBR. " + "; ".join(problems))
