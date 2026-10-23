"""Backends: Live (PSRP to SCVMM and VBR) and Mock (in-memory demo estate)."""
from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .remote import RemoteError, load_script, run_script


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def normalise_scvmm(data: dict) -> tuple[list[dict], list[dict]]:
    clouds = _as_list(data.get("clouds"))
    vms = []
    for v in _as_list(data.get("vms")):
        v["tags"] = [t for t in _as_list(v.get("tags")) if t]
        v["vm_guid"] = (v.get("vm_guid") or "").strip("{} ").lower() or None
        vms.append(v)
    return clouds, vms


def normalise_vbr(data: dict) -> dict:
    jobs = []
    for j in _as_list(data.get("jobs")):
        j["objects"] = _as_list(j.get("objects"))
        for o in j["objects"]:
            o["refs"] = [r for r in _as_list(o.get("refs")) if r]
        jobs.append(j)
    return {
        "jobs": jobs,
        "copy_jobs": _as_list(data.get("copy_jobs")),
        "foreign_jobs": _as_list(data.get("foreign_jobs")),
        "all_job_names": [n for n in _as_list(data.get("all_job_names")) if n],
        "repositories": [r for r in _as_list(data.get("repositories")) if r],
    }


class LiveBackend:
    name = "live"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def _need(self, which: str):
        target = getattr(self.cfg, which)
        if not target:
            raise RemoteError(f"No '{which}' host configured in config.yaml.")
        return target

    def scvmm_inventory(self) -> tuple[list[dict], list[dict]]:
        data = run_script(self._need("scvmm"), load_script("scvmm_inventory.ps1"), {
            "VmmServer": self.cfg.scvmm_server_name,
            "CustomPropertyName": self.cfg.classification.custom_property,
        })
        return normalise_scvmm(data)

    def vbr_inventory(self) -> dict:
        data = run_script(self._need("vbr"), load_script("vbr_inventory.ps1", True), {})
        return normalise_vbr(data)

    def apply(self, operations: list[dict]) -> list[dict]:
        data = run_script(self._need("vbr"), load_script("vbr_apply.ps1", True), {"operations": operations})
        return _as_list(data.get("results"))

    def probe(self) -> dict:
        out: dict[str, Any] = {}
        for key, script, common in (("scvmm", "scvmm_probe.ps1", False), ("vbr", "vbr_probe.ps1", True)):
            try:
                out[key] = {"ok": True, "data": run_script(self._need(key), load_script(script, common), {
                    "VmmServer": self.cfg.scvmm_server_name,
                    "CustomPropertyName": self.cfg.classification.custom_property,
                })}
            except Exception as exc:  # noqa: BLE001
                out[key] = {"ok": False, "error": str(exc)}
        return out


class MockBackend:
    """A believable multi-tenant estate for demos and UI work. State persists in data_dir.

    It includes VMware and Nutanix jobs that share the server. The mock enforces the same
    ownership rule as the PowerShell side, so a bug that tried to touch them would fail here too.
    """

    name = "mock"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.path = Path(cfg.data_dir) / "mock_state.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = self._seed()
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1))

    def _seed(self) -> dict:
        rnd = random.Random(42)
        tenants = ["Acme Legal", "Brightwell Care", "Castle Logistics", "Dunmore Retail", "Everline Finance", "Foxglove Schools"]
        clouds, vms = [], []
        hosts = [f"HV{n:02d}.arc.local" for n in range(1, 9)]
        for t in tenants:
            cid = str(uuid.UUID(int=rnd.getrandbits(128)))
            clouds.append({"id": cid, "name": t, "description": f"{t} tenant cloud"})
            prefix = "".join(w[0] for w in t.split()).upper()
            for i in range(rnd.randint(6, 16)):
                role = rnd.choice(["DC", "APP", "SQL", "FS", "RDS", "WEB", "APP"])
                big = role in ("FS", "SQL") and rnd.random() < 0.6
                vms.append({
                    "id": str(uuid.UUID(int=rnd.getrandbits(128))),
                    "vm_guid": str(uuid.UUID(int=rnd.getrandbits(128))),
                    "name": f"{prefix}-{role}{i + 1:02d}", "cloud_id": cid, "cloud_name": t,
                    "host": rnd.choice(hosts), "cluster": "HVCL01.arc.local",
                    "status": rnd.choice(["Running"] * 9 + ["PowerOff"]), "in_library": False,
                    "max_gb": round(rnd.uniform(2200, 8000) if big else rnd.uniform(60, 600), 1),
                    "used_gb": 0, "custom_property": "LargeData" if big and rnd.random() < 0.5 else None, "tags": [],
                })
        for v in vms:
            v["used_gb"] = round(v["max_gb"] * random.Random(v["name"]).uniform(0.3, 0.8), 1)
        vms[12]["tags"] = ["LargeData"]
        acme = [v for v in vms if v["cloud_name"] == "Acme Legal"][:3]
        jobs = [
            {"id": str(uuid.uuid4()), "name": "Legacy Acme Daily", "description": "Built by hand", "type": "Hyper-V Backup",
             "enabled": True, "schedule_enabled": True, "start_time": "18:30", "retention_type": "Days",
             "retention_days": 14, "repository": "REPO-01",
             "objects": [{"name": v["name"], "kind": "vm", "include": True, "refs": [v["vm_guid"]]} for v in acme]},
            {"id": str(uuid.uuid4()), "name": "Infra HV08 host", "description": "Management VMs", "type": "Hyper-V Backup",
             "enabled": True, "schedule_enabled": True, "start_time": "22:00", "retention_type": "Days",
             "retention_days": 30, "repository": "REPO-01",
             "objects": [{"name": "HV08.arc.local", "kind": "container", "include": True, "refs": []}]},
        ]
        foreign = [
            {"id": str(uuid.uuid4()), "name": "VMW-Prod-SQL", "type": "VMware Backup", "platform": "EVmware",
             "start_time": "18:00", "schedule_enabled": True},
            {"id": str(uuid.uuid4()), "name": "VMW-Prod-General", "type": "VMware Backup", "platform": "EVmware",
             "start_time": "19:30", "schedule_enabled": True},
            {"id": str(uuid.uuid4()), "name": "NTNX-AHV-Nightly", "type": "Nutanix AHV Backup", "platform": "ENutanix",
             "start_time": "20:15", "schedule_enabled": True},
            {"id": str(uuid.uuid4()), "name": "VMW-Offsite-Copy", "type": "Backup Copy", "platform": "",
             "start_time": None, "schedule_enabled": False},
        ]
        return {"clouds": clouds, "vms": vms, "jobs": jobs, "foreign": foreign, "copy_jobs": [], "points": {},
                "repositories": ["REPO-01", "SOBR-Tenants", "SOBR-Copy-7d", "SOBR-VMware"]}

    def scvmm_inventory(self):
        st = self.state
        # Pretend last night's jobs ran: every VM in a managed job now has restore points there.
        for j in st["jobs"]:
            if "[hvbo cloud=" in (j.get("description") or ""):
                st["points"][j["id"]] = sorted({o["name"] for o in j["objects"]})
        r = random.random()
        if r < 0.45:  # a new VM appears in a tenant cloud
            cloud = random.choice(st["clouds"])
            n = sum(1 for v in st["vms"] if v["cloud_id"] == cloud["id"]) + 1
            prefix = "".join(w[0] for w in cloud["name"].split()).upper()
            st["vms"].append({
                "id": str(uuid.uuid4()), "vm_guid": str(uuid.uuid4()), "name": f"{prefix}-NEW{n:02d}",
                "cloud_id": cloud["id"], "cloud_name": cloud["name"], "host": "HV03.arc.local",
                "cluster": "HVCL01.arc.local", "status": "Running", "in_library": False,
                "max_gb": 127.0, "used_gb": 40.0, "custom_property": None, "tags": [],
            })
        elif r < 0.8:  # someone sets BackupTier=LargeData on a VM already in a 30-day job
            in_std = {o["refs"][0] for j in st["jobs"] if "tier=standard]" in (j.get("description") or "")
                      for o in j["objects"] if o["refs"]}
            cands = [v for v in st["vms"] if v["vm_guid"] in in_std and not v.get("custom_property")]
            if cands:
                random.choice(cands)["custom_property"] = "LargeData"
        self._save()
        return json.loads(json.dumps(st["clouds"])), json.loads(json.dumps(st["vms"]))

    def vbr_inventory(self) -> dict:
        st = self.state
        names = [j["name"] for j in st["jobs"]] + [c["name"] for c in st["copy_jobs"]] + [f["name"] for f in st["foreign"]]
        return {"jobs": json.loads(json.dumps(st["jobs"])), "copy_jobs": json.loads(json.dumps(st["copy_jobs"])),
                "foreign_jobs": [{k: f[k] for k in ("name", "type", "platform", "start_time", "schedule_enabled")}
                                 for f in st["foreign"]],
                "all_job_names": names, "repositories": list(st["repositories"])}

    # ---- same ownership rule as Assert-HvboOwned
    @staticmethod
    def _owned(job: dict | None, kind: str = "backup") -> dict:
        if not job:
            raise RuntimeError("SAFETY: target job not found; refusing to continue.")
        marker = "[hvbo-copy cloud=" if kind == "copy" else "[hvbo cloud="
        if marker not in (job.get("description") or "") or (kind == "backup" and "Hyper-V" not in job.get("type", "")):
            raise RuntimeError(f"SAFETY: '{job['name']}' is not managed by the orchestrator. It was not changed.")
        return job

    def _find(self, job_id=None, name=None) -> dict | None:
        everything = self.state["jobs"] + self.state["foreign"] + self.state["copy_jobs"]
        return next((j for j in everything if (job_id and j["id"] == job_id) or (not job_id and name and j["name"] == name)), None)

    def apply(self, operations: list[dict]) -> list[dict]:
        st = self.state
        results = []
        for op in operations:
            res = {"id": op["id"], "op": op["op"], "ok": True, "messages": [], "job_id": op.get("job_id"), "job_name": op.get("job_name")}
            try:
                kind = op["op"]
                if kind == "ensure_job":
                    job = self._owned(self._find(op["job_id"])) if op.get("job_id") else None
                    if not job:
                        clash = self._find(name=op["job_name"])
                        job = self._owned(clash) if clash else None
                    have = {r for o in (job or {}).get("objects", []) for r in o["refs"]}
                    objs = [{"name": v["name"], "kind": "vm", "include": True, "refs": [v["vm_guid"]]}
                            for v in op["vms"] if v["vm_guid"] not in have]
                    if job:
                        job["objects"].extend(objs)
                        res["messages"].append(f"Added {len(objs)} VM(s) to '{job['name']}'.")
                    else:
                        job = {"id": str(uuid.uuid4()), "name": op["job_name"], "description": op["description"],
                               "type": "Hyper-V Backup", "enabled": True, "schedule_enabled": bool(op.get("start_time")),
                               "start_time": op.get("start_time"), "retention_type": "Days",
                               "retention_days": op["retention_days"], "repository": op["repository"], "objects": objs}
                        st["jobs"].append(job)
                        res["messages"].append(f"Created job '{job['name']}' with {len(objs)} VM(s) on '{op['repository']}'.")
                elif kind == "set_schedule":
                    job = self._owned(self._find(op["job_id"]))
                    job["start_time"], job["schedule_enabled"] = op["start_time"], True
                    res["messages"].append(f"Daily schedule set to {op['start_time']}.")
                elif kind == "retier_vm":
                    src = self._owned(self._find(op["from_job_id"]))
                    job = self._owned(self._find(op.get("job_id"), op["job_name"]))
                    for v in op["vms"]:
                        pts = st["points"].get(src["id"], [])
                        state = "no restore points to move"
                        if op.get("remap") and v["name"] in pts:
                            pts.remove(v["name"])
                            st["points"].setdefault(job["id"], []).append(v["name"])
                            state = "restore points moved"
                        if not any(v["vm_guid"] in o["refs"] for o in job["objects"]):
                            job["objects"].append({"name": v["name"], "kind": "vm", "include": True, "refs": [v["vm_guid"]]})
                        src["objects"] = [o for o in src["objects"] if v["vm_guid"] not in o["refs"]]
                        res["messages"].append(f"'{v['name']}': {state}; now in '{job['name']}', removed from '{src['name']}'.")
                elif kind == "ensure_copy":
                    clash = self._find(name=op["copy_name"])
                    if clash:
                        job = self._owned(clash, "copy")
                        res["messages"].append(f"Copy job '{op['copy_name']}' already exists.")
                    else:
                        src = self._owned(self._find(op.get("source_job_id"), op["source_job_name"]))
                        if src.get("repository") == op["repository"]:
                            raise RuntimeError("Copy target is the source job's own repository.")
                        job = {"id": str(uuid.uuid4()), "name": op["copy_name"], "description": op["description"],
                               "mode": op["mode"], "repository": op["repository"], "retention_days": op["retention_days"],
                               "source": src["id"]}
                        st["copy_jobs"].append(job)
                        res["messages"].append(f"Created {op['mode']} backup copy job '{job['name']}' from '{src['name']}' "
                                               f"to '{op['repository']}', keeping {op['retention_days']} days.")
                        res["verify"] = {"retention_type": "RestoreDays", "retention_days": op["retention_days"], "mode": op["mode"]}
                else:
                    raise RuntimeError(f"Unknown operation '{kind}'.")
                res.update(job_id=job["id"], job_name=job["name"])
                if kind in ("ensure_job", "set_schedule"):
                    res["verify"] = {"start_time": job["start_time"], "schedule_enabled": job["schedule_enabled"],
                                     "retention_type": job["retention_type"], "retention_days": job["retention_days"],
                                     "object_count": len(job["objects"])}
            except Exception as exc:  # noqa: BLE001
                res["ok"] = False
                res["messages"].append(str(exc))
            results.append(res)
        self._save()
        return results

    def probe(self) -> dict:
        return {"scvmm": {"ok": True, "data": {"mode": "mock", "clouds": [c["name"] for c in self.state["clouds"]]}},
                "vbr": {"ok": True, "data": {"mode": "mock", "hyperv_jobs": len(self.state["jobs"]),
                                             "other_jobs_untouched": [f["name"] for f in self.state["foreign"]]}}}


def make_backend(cfg: AppConfig):
    return MockBackend(cfg) if cfg.mock else LiveBackend(cfg)
