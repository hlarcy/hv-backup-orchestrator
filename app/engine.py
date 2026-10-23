"""Orchestration: discover, plan, and (optionally) apply. One run at a time."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from .backend import make_backend
from .config import AppConfig
from .db import Store
from .planner import build_plan
from .safety import validate_ops

log = logging.getLogger("hvbo.engine")
PLAN_MAX_AGE_SECONDS = 30 * 60


class RunLog:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        self.lines.append(line)
        log.info(msg)

    def text(self) -> str:
        return "\n".join(self.lines)


class Engine:
    def __init__(self, cfg: AppConfig, store: Store):
        self.cfg = cfg
        self.store = store
        self.backend = make_backend(cfg)
        self._lock = threading.Lock()
        self.current_run: int | None = None

    # ---- runtime toggles (stored in DB, default from config)
    @property
    def auto_apply(self) -> bool:
        return bool(self.store.get("auto_apply", self.cfg.auto_apply))

    @property
    def paused(self) -> bool:
        return bool(self.store.get("paused", False))

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # ---- entry points
    def start(self, trigger: str, mode: str, **kw) -> int | None:
        """Kick off a run in a background thread. Returns run id, or None if one is already running."""
        if not self._lock.acquire(blocking=False):
            return None
        run_id = self.store.start_run(trigger, mode)
        self.current_run = run_id
        threading.Thread(target=self._run, args=(run_id, trigger, mode), kwargs=kw, daemon=True).start()
        return run_id

    def scheduled_tick(self) -> None:
        if self.paused:
            log.info("Scheduled sync skipped: paused.")
            return
        self.start("schedule", "apply" if self.auto_apply else "plan")

    # ---- the run itself
    def _run(self, run_id: int, trigger: str, mode: str, rebalance: bool = False,
             include_manual: bool = False, from_run: int | None = None) -> None:
        rl = RunLog()
        try:
            if mode == "apply_stored":
                plan = self._stored_plan(from_run, rl)
            else:
                plan = self._discover_and_plan(rl, rebalance)
            self.store.update_run(run_id, plan=plan, summary=plan["summary"], log=rl.text())

            results: list[dict] = []
            if mode in ("apply", "apply_stored"):
                results = self._apply(plan, rl, include_manual)
            ok = all(r.get("ok") for r in results)
            status = "planned" if mode == "plan" else ("applied" if ok else "partial")
            if mode != "plan" and not results:
                status = "no_changes"
            summary = {**plan["summary"], "applied": sum(1 for r in results if r.get("ok")),
                       "failed": sum(1 for r in results if not r.get("ok"))}
            self.store.finish_run(run_id, status, summary=summary, results=results, log=rl.text())
        except Exception as exc:  # noqa: BLE001
            rl(f"ERROR: {exc}")
            log.error(traceback.format_exc())
            self.store.finish_run(run_id, "failed", error=str(exc), log=rl.text())
        finally:
            self.current_run = None
            self.store.prune()
            self._lock.release()

    def _discover_and_plan(self, rl: RunLog, rebalance: bool) -> dict:
        t = time.monotonic()
        rl(f"Discovering SCVMM clouds and VMs ({self.backend.name})")
        clouds, vms = self.backend.scvmm_inventory()
        rl(f"SCVMM: {len(clouds)} clouds, {len(vms)} cloud VMs ({time.monotonic() - t:.1f}s)")
        t = time.monotonic()
        rl("Reading Veeam jobs (read only)")
        inv = self.backend.vbr_inventory()
        jobs, repos = inv["jobs"], inv["repositories"]
        rl(f"Veeam: {len(jobs)} Hyper-V backup jobs, {len(inv['copy_jobs'])} managed copy jobs, "
           f"{len(inv['foreign_jobs'])} other jobs (VMware, Nutanix, agent and so on, never modified), "
           f"{len(repos)} repositories ({time.monotonic() - t:.1f}s)")

        plan = build_plan(self.cfg, clouds, vms, jobs, self.store.slot_table(), rebalance=rebalance,
                          copy_jobs=inv["copy_jobs"], foreign_jobs=inv["foreign_jobs"],
                          all_job_names=inv["all_job_names"])
        repo_names = {r.lower() for r in repos}
        for a in plan["actions"]:
            if a.get("blocked") or not repo_names:
                continue
            if a.get("repository") and a["repository"].lower() not in repo_names and (
                    a["type"] == "create_copy" or not a.get("exists")):
                a["blocked"] = f"Repository '{a['repository']}' does not exist in VBR."
        plan["inventory"] = {
            "clouds": clouds,
            "jobs": [{k: j.get(k) for k in ("id", "name", "description", "start_time", "schedule_enabled",
                                            "retention_days", "retention_type", "repository", "managed_key")}
                     | {"vm_count": len(j.get("objects") or [])} for j in jobs],
            "foreign_jobs": inv["foreign_jobs"],
            "repositories": repos,
        }
        for w in plan["warnings"]:
            rl(f"Warning: {w}")
        rl(f"Plan: {len(plan['actions'])} action(s), {plan['summary']['status'].get('pending', 0)} VM(s) to add, "
           f"{plan['summary']['status'].get('retier', 0)} VM(s) to move between tiers")
        existing = {j.get("managed_key") for j in jobs if j.get("managed_key")}
        if not rebalance:
            self.store.save_slots({k: s for k, s in plan["slot_table"].items() if k in existing})
        return plan

    def _stored_plan(self, run_id: int | None, rl: RunLog) -> dict:
        src = self.store.run(run_id) if run_id else self.store.latest_with_plan()
        if not src or not src.get("plan"):
            raise RuntimeError("No stored plan to apply.")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(src["plan"]["generated_at"])).total_seconds()
        if age > PLAN_MAX_AGE_SECONDS:
            raise RuntimeError(f"Plan from run #{src['id']} is {age / 60:.0f} minutes old. Run discovery again first.")
        rl(f"Applying stored plan from run #{src['id']} ({age / 60:.0f} min old)")
        return src["plan"]

    def _send(self, ops: list[dict], plan: dict, rl: RunLog, label: str) -> list[dict]:
        if not ops:
            return []
        validate_ops(ops, plan)  # raises SafetyError: nothing is sent
        rl(f"{label}: {len(ops)} operation(s)")
        results = self.backend.apply(ops)
        for r in results:
            rl(f"{'OK  ' if r.get('ok') else 'FAIL'} {r.get('op')} {r.get('job_name')}: {' '.join(r.get('messages') or [])}")
        return results

    def _apply(self, plan: dict, rl: RunLog, include_manual: bool) -> list[dict]:
        todo = [a for a in plan["actions"] if not a.get("blocked") and (a["auto"] or include_manual)]
        for a in plan["actions"]:
            if a.get("blocked"):
                rl(f"Blocked: {a['job_name']}: {a['blocked']}")
            elif not (a["auto"] or include_manual):
                rl(f"Held for approval: {a['type']} {a['job_name']}")
        if not todo:
            rl("Nothing to apply.")
            return []

        def ensure_op(a: dict, vms: list[dict]) -> dict:
            return {"id": a["id"], "op": "ensure_job", "job_id": a.get("job_id"), "job_name": a["job_name"],
                    "description": a.get("description"), "repository": a.get("repository"),
                    "retention_days": a.get("retention_days"), "start_time": a.get("start_time"),
                    "template_job": a.get("template_job"), "vms": vms}

        # Phase 1: create jobs, add VMs, fix schedules.
        p1 = []
        for a in todo:
            if a["type"] in ("create_job", "add_vms"):
                p1.append(ensure_op(a, a["vms"]))
            elif a["type"] == "move_vms" and a.get("create_target"):
                p1.append(ensure_op(a, a["vms"]))
            elif a["type"] == "set_schedule":
                p1.append({"id": a["id"], "op": "set_schedule", "job_id": a["job_id"], "job_name": a["job_name"],
                           "start_time": a["start_time"]})
        p2_all = [a for a in todo if a["type"] == "move_vms"]
        p3_all = [a for a in todo if a["type"] == "create_copy"]

        def retier_op(a: dict) -> dict:
            return {"id": a["id"] + "-mv", "op": "retier_vm", "from_job_id": a["from_job_id"],
                    "from_job_name": a["from_job_name"], "job_id": a.get("job_id"), "job_name": a["job_name"],
                    "remap": a.get("remap_backups", True), "vms": a["vms"]}

        def copy_op(a: dict) -> dict:
            return {"id": a["id"], "op": "ensure_copy", "source_job_id": a.get("source_job_id"),
                    "source_job_name": a["source_job_name"], "copy_name": a["job_name"],
                    "description": a["description"], "repository": a["repository"],
                    "retention_days": a["retention_days"], "mode": a["mode"]}

        # Check every phase before anything is sent, so a refusal never leaves a half-applied plan.
        validate_ops(p1 + [retier_op(a) for a in p2_all] + [copy_op(a) for a in p3_all], plan)
        rl("Safety check passed: every operation targets an orchestrator-managed job or a new, unique name.")

        results = self._send(p1, plan, rl, "Phase 1, jobs and schedules")
        ok = {r["id"] for r in results if r.get("ok")}
        created_keys = {a["job_key"] for a in todo if a["type"] in ("create_job", "move_vms")
                        and (a["type"] == "create_job" or a.get("create_target")) and a["id"] in ok}

        # Phase 2: tier moves. Move restore points with the VM, then take it out of the old job.
        p2 = []
        for a in todo:
            if a["type"] != "move_vms":
                continue
            if not a.get("exists") and a["job_key"] not in created_keys:
                rl(f"Skipped move into '{a['job_name']}': the job was not created.")
                continue
            p2.append(retier_op(a))
        results += self._send(p2, plan, rl, "Phase 2, tier moves")

        # Phase 3: backup copy jobs for new and existing managed jobs.
        p3 = []
        for a in todo:
            if a["type"] != "create_copy":
                continue
            if not a.get("source_job_id") and a["job_key"] not in created_keys:
                rl(f"Skipped copy job '{a['job_name']}': its source job was not created.")
                continue
            p3.append(copy_op(a))
        results += self._send(p3, plan, rl, "Phase 3, backup copy jobs")

        by_id = {a["id"]: a for a in todo}
        new_slots, names = {}, {}
        for r in results:
            a = by_id.get(r["id"])
            if a and r.get("ok") and a["type"] != "create_copy" and a.get("slot") is not None and a["slot"] >= 0:
                new_slots[a["job_key"]] = a["slot"]
                names[a["job_key"]] = r.get("job_name")
        if plan.get("rebalance") and all(r.get("ok") for r in results):
            self.store.save_slots(plan["slot_table"], replace=True)
        if new_slots:
            self.store.save_slots(new_slots, names)
        return results
