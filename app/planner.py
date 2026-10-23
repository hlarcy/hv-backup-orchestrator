"""Pure planning logic: inventory in, list of actions out.

Nothing here talks to SCVMM or Veeam, so it is fully unit-testable.

Inputs (normalised dicts, see remote/*.ps1 for the producers):
  clouds: [{id, name}]
  vms:    [{id, vm_guid, name, cloud_id, cloud_name, host, cluster, status,
            max_gb, used_gb, custom_property, tags, in_library}]
  jobs:   [{id, name, description, enabled, schedule_enabled, start_time,
            retention_days, repository, objects: [{name, kind, include, refs}]}]
  slot_table: {job_key: slot}
"""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .slots import SlotAllocator, Window

MARKER_RE = re.compile(r"\[hvbo\s+cloud=(?P<cloud>[^\s\]]+)\s+tier=(?P<tier>[^\s\]]+)\]", re.I)
GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\[\]]')


def norm_guid(value: Any) -> str | None:
    if not value:
        return None
    m = GUID_RE.search(str(value))
    return m.group(0).lower() if m else None


def normalise_tier_key(tier: str, cfg: AppConfig) -> str:
    """Tier keys are 'd<days>'. Markers from earlier versions ('standard', 'large') are mapped."""
    from .config import LEGACY_TIER_DAYS
    if re.fullmatch(r"d\d+", tier or ""):
        return cfg.tier_for_days(int(tier[1:]))
    if tier == "standard":
        return cfg.default_tier
    if tier in LEGACY_TIER_DAYS:
        return cfg.tier_for_days(LEGACY_TIER_DAYS[tier])
    return tier


def job_key(cloud_id: str, tier: str) -> str:
    return f"{cloud_id}|{tier}"


def marker(cloud_id: str, tier: str) -> str:
    return f"[hvbo cloud={cloud_id} tier={tier}]"


COPY_MARKER_RE = re.compile(r"\[hvbo-copy\s+cloud=(?P<cloud>[^\s\]]+)\s+tier=(?P<tier>[^\s\]]+)\]", re.I)


def copy_marker(cloud_id: str, tier: str) -> str:
    return f"[hvbo-copy cloud={cloud_id} tier={tier}]"


def parse_copy_marker(description: str | None) -> tuple[str, str] | None:
    m = COPY_MARKER_RE.search(description or "")
    return (m.group("cloud"), m.group("tier")) if m else None


def parse_marker(description: str | None) -> tuple[str, str] | None:
    if not description:
        return None
    m = MARKER_RE.search(description)
    return (m.group("cloud"), m.group("tier")) if m else None


# --------------------------------------------------------------------------- classification

DAYS_RE = re.compile(r"^\s*(\d+)\s*$")


def classify(vm: dict, cfg: AppConfig) -> tuple[str, str]:
    """Return (tier, reason). The tier is always a retention in days.

    The custom property holds the retention. A whole number above 0 is used exactly as given,
    shorter or longer than the default. Blank, missing or anything else gives the default.
    """
    name = cfg.classification.custom_property
    prop = (vm.get("custom_property") or "").strip() if name else ""
    if not prop:
        return cfg.default_tier, "Not set: default"
    m = DAYS_RE.match(prop)
    if m and int(m.group(1)) > 0:
        return cfg.tier_for_days(int(m.group(1))), f"{name}={prop}"
    return cfg.default_tier, f"{name}={prop} is not a whole number of days: default"


# --------------------------------------------------------------------------- coverage

@dataclass
class CoverageIndex:
    by_guid: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_name: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    containers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    excl_guid: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    excl_name: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def build(cls, jobs: list[dict]) -> "CoverageIndex":
        idx = cls()
        for job in jobs:
            jid = job["id"]
            for obj in job.get("objects") or []:
                name = (obj.get("name") or "").lower()
                guids = {g for g in (norm_guid(r) for r in obj.get("refs") or []) if g}
                if not obj.get("include", True):
                    for g in guids:
                        idx.excl_guid[g].add(jid)
                    if name:
                        idx.excl_name[name].add(jid)
                    continue
                if obj.get("kind") == "vm":
                    for g in guids:
                        idx.by_guid[g].add(jid)
                    if name:
                        idx.by_name[name].add(jid)
                elif name:
                    idx.containers[name].add(jid)
        return idx

    def covering_jobs(self, vm: dict, name_is_unique: bool) -> list[tuple[str, str]]:
        """List of (job_id, mode) that back this VM up."""
        out: dict[str, str] = {}
        guid = vm.get("vm_guid")
        name = (vm.get("name") or "").lower()
        if guid:
            for j in self.by_guid.get(guid, ()):
                out[j] = "explicit"
        if name_is_unique:
            for j in self.by_name.get(name, ()):
                out.setdefault(j, "name")
        for container in (vm.get("host"), vm.get("cluster")):
            if not container:
                continue
            for j in self.containers.get(container.lower(), ()):
                out.setdefault(j, "container")
            short = container.split(".")[0].lower()
            for j in self.containers.get(short, ()):
                out.setdefault(j, "container")
        # Drop container coverage where the VM is explicitly excluded from that job.
        for j, mode in list(out.items()):
            if mode == "container" and (
                (guid and j in self.excl_guid.get(guid, ())) or j in self.excl_name.get(name, ())
            ):
                del out[j]
        return list(out.items())


# --------------------------------------------------------------------------- planning

def make_job_name(cfg: AppConfig, cloud_name: str, tier: str, taken: set[str]) -> str:
    t = cfg.tiers.get(tier)
    raw = cfg.job_name_template.format(
        cloud=cloud_name, tier=tier, tier_label=t.label if t else tier,
        retention=t.retention_days if t else "",
    )
    name = BAD_NAME_CHARS.sub("-", raw).strip()[: cfg.job_name_max_length].strip()
    base, n = name, 2
    while name.lower() in taken:
        suffix = f" {n}"
        name = base[: cfg.job_name_max_length - len(suffix)] + suffix
        n += 1
    taken.add(name.lower())
    return name


def _vm_ref(vm: dict) -> dict:
    return {k: vm.get(k) for k in ("vm_guid", "name", "host", "cluster", "id")}


def make_copy_name(cfg: AppConfig, source_name: str, cloud_name: str, tier: str, taken: set[str]) -> str:
    t = cfg.tiers.get(tier)
    raw = cfg.backup_copy.name_template.format(job=source_name, cloud=cloud_name, tier=tier,
                                               tier_label=t.label if t else tier)
    name = BAD_NAME_CHARS.sub("-", raw).strip()[: cfg.job_name_max_length + 10].strip()
    base, n = name, 2
    while name.lower() in taken:
        name = f"{base} {n}"
        n += 1
    taken.add(name.lower())
    return name


def build_plan(
    cfg: AppConfig,
    clouds: list[dict],
    vms: list[dict],
    jobs: list[dict],
    slot_table: dict[str, int] | None = None,
    rebalance: bool = False,
    copy_jobs: list[dict] | None = None,
    foreign_jobs: list[dict] | None = None,
    all_job_names: list[str] | None = None,
) -> dict:
    """Build the action plan.

    jobs          Hyper-V backup jobs (managed and hand-built). Hand-built ones are read only.
    copy_jobs     Backup copy jobs carrying the hvbo-copy marker (only ours are ever returned).
    foreign_jobs  Every other job on the VBR server (VMware, Nutanix, agents, copies...). Read only:
                  used for start-time reservation and display, never for actions.
    all_job_names Every job name on the server, so new names can never collide with an existing job.
    """
    slot_table = dict(slot_table or {})
    copy_jobs = copy_jobs or []
    foreign_jobs = foreign_jobs or []
    window = Window(cfg.schedule.window_start, cfg.schedule.window_end, cfg.schedule.stagger_minutes)
    warnings: list[str] = []

    cloud_by_id = {c["id"]: c for c in clouds}

    def retention(tier: str) -> int:
        t = cfg.tiers.get(tier)
        return t.retention_days if t else 0

    def cloud_name_of(cid: str) -> str:
        return cloud_by_id.get(cid, {}).get("name", cid)

    def in_scope(cloud_name: str) -> bool:
        if cfg.include_clouds and cloud_name not in cfg.include_clouds:
            return False
        return cloud_name not in cfg.exclude_clouds

    # ---------------- ownership: only jobs carrying our marker are ever modified
    jobs_by_id = {j["id"]: j for j in jobs}
    managed: dict[str, dict] = {}
    for j in jobs:
        m = parse_marker(j.get("description"))
        if not m:
            continue
        m = (m[0], normalise_tier_key(m[1], cfg))
        key = job_key(*m)
        if key in managed:
            warnings.append(f"Two jobs claim {key}: '{managed[key]['name']}' and '{j['name']}'. Using the first.")
            continue
        j["managed_key"] = key
        managed[key] = j

    managed_ids = {j["id"] for j in managed.values()}
    managed_copies: dict[str, dict] = {}
    for cj in copy_jobs:
        m = parse_copy_marker(cj.get("description"))
        if m:
            managed_copies.setdefault(job_key(m[0], normalise_tier_key(m[1], cfg)), cj)
    taken_names = {n.lower() for n in (all_job_names or [])} | {j["name"].lower() for j in jobs} \
        | {c["name"].lower() for c in copy_jobs} | {f["name"].lower() for f in foreign_jobs}

    # ---------------- slots
    reserved: set[int] = set()
    reserved_names: dict[int, list[str]] = defaultdict(list)

    def reserve(name: str, start: str | None, enabled: bool) -> None:
        if enabled and start:
            s = window.slot_covering(start)
            if s is not None:
                reserved.add(s)
                reserved_names[s].append(name)

    if cfg.schedule.avoid_unmanaged_job_times:
        for j in jobs:
            if j["id"] not in managed_ids:
                reserve(j["name"], j.get("start_time"), j.get("schedule_enabled"))
    if cfg.safety.reserve_foreign_job_times:
        for f in foreign_jobs:
            reserve(f"{f['name']} ({f.get('platform') or f.get('type') or 'other'})", f.get("start_time"),
                    f.get("schedule_enabled"))
    alloc = SlotAllocator(window, reserved)

    if rebalance:
        longest = cfg.schedule.order in ("large_first", "longest_first")
        order = sorted(managed, key=lambda k: (-retention(k.split("|")[1]) if longest else 0,
                                               cloud_name_of(k.split("|")[0]).lower(), k.split("|")[1]))
    else:
        order = sorted(managed, key=lambda k: (k not in slot_table, k))
    for key in order:
        job = managed[key]
        if not rebalance:
            s = slot_table.get(key)
            if s is not None and alloc.claim(s, key):
                continue
            s = window.slot_for_time(job.get("start_time"))
            if s is not None and alloc.claim(s, key):
                continue
        if alloc.first_free(key) is None:
            warnings.append(f"No free start slot for existing job '{job['name']}'.")

    # ---------------- VM classification and coverage
    name_counts: dict[str, int] = defaultdict(int)
    for vm in vms:
        name_counts[(vm.get("name") or "").lower()] += 1
    idx = CoverageIndex.build(jobs)
    no_ref = sum(1 for j in jobs for o in j.get("objects") or []
                 if o.get("kind") == "vm" and not any(norm_guid(r) for r in o.get("refs") or []))
    if no_ref:
        warnings.append(f"{no_ref} VM object(s) in Veeam jobs expose no VM GUID; they are matched by unique name only.")

    vm_status: list[dict] = []
    pending: dict[str, list[dict]] = defaultdict(list)                  # job_key -> VMs to add
    moves: dict[tuple[str, str], list[dict]] = defaultdict(list)       # (from_job_id, to_key) -> VMs

    for vm in sorted(vms, key=lambda v: (v.get("cloud_name") or "", v.get("name") or "")):
        cloud_name = vm.get("cloud_name") or cloud_name_of(vm.get("cloud_id"))
        tier, reason = classify(vm, cfg)
        cover = idx.covering_jobs(vm, name_counts[(vm.get("name") or "").lower()] == 1)
        covered_by = [{"job_id": j, "job_name": jobs_by_id[j]["name"], "mode": m,
                       "managed_key": jobs_by_id[j].get("managed_key")} for j, m in cover]
        row = {**vm, "power": vm.get("status"), "cloud_name": cloud_name, "tier": tier,
               "tier_reason": reason, "covered_by": covered_by}
        want = job_key(vm.get("cloud_id"), tier)
        managed_cover = [c for c in covered_by if c["managed_key"]]
        unmanaged_cover = [c for c in covered_by if not c["managed_key"]]
        right = [c for c in managed_cover if c["managed_key"] == want]
        wrong_same = [c for c in managed_cover if c["managed_key"] != want
                      and c["managed_key"].split("|")[0] == vm.get("cloud_id")]
        wrong_other = [c for c in managed_cover if c["managed_key"].split("|")[0] != vm.get("cloud_id")]

        if not in_scope(cloud_name):
            row["status"] = "out_of_scope"
        elif vm.get("in_library"):
            row["status"] = "library"
        elif tier == "exclude":
            row["status"] = "excluded"
        elif not cfg.include_stopped_vms and (vm.get("status") or "").lower() not in ("running", ""):
            row["status"] = "skipped_stopped"
        elif not vm.get("vm_guid") and not covered_by:
            row["status"] = "no_guid"
            warnings.append(f"VM '{vm['name']}' has no Hyper-V VMId in SCVMM; skipped.")
        elif wrong_same or (wrong_other and not right):
            # VM sits in one of our jobs for the wrong tier (or wrong cloud): move it, with its backups.
            row["status"] = "retier" if wrong_same else "wrong_cloud"
            for c in (wrong_same or wrong_other):
                moves[(c["job_id"], want)].append(vm)
        elif right:
            row["status"] = "protected"
        elif unmanaged_cover and cfg.coverage_mode != "managed_only" and tier == cfg.default_tier:
            row["status"] = "protected"
            row["note"] = "Covered by a hand-built job (left untouched)"
        else:
            row["status"] = "pending"
            if unmanaged_cover:
                row["note"] = f"Also in hand-built job '{unmanaged_cover[0]['job_name']}' (left untouched)"
            pending[want].append(vm)
        if len(covered_by) > 1 and "note" not in row:
            row["note"] = "In more than one job"
        vm_status.append(row)

    # ---------------- actions
    actions: list[dict] = []
    new_slots: dict[str, int] = {}
    new_names: dict[str, str] = {}

    def target_spec(key: str) -> dict:
        cloud_id, tier = key.split("|")
        cname = cloud_name_of(cloud_id)
        t = cfg.tiers[tier]
        spec = {"job_key": key, "cloud_id": cloud_id, "cloud_name": cname, "tier": tier,
                "tier_label": t.label, "retention_days": t.retention_days,
                "repository": cfg.repository_for(cname, tier)}
        if key in managed:
            j = managed[key]
            spec.update(job_id=j["id"], job_name=j["name"], exists=True, repository=j.get("repository") or spec["repository"])
            return spec
        if key not in new_slots:
            s = alloc.first_free(key)
            new_slots[key] = s if s is not None else -1
        if key not in new_names:
            new_names[key] = make_job_name(cfg, cname, tier, taken_names)
        slot = new_slots[key]
        spec.update(job_id=None, exists=False, job_name=new_names[key], slot=slot,
                    start_time=window.slot_time(slot) if slot >= 0 else None,
                    description=f"Managed by HV Backup Orchestrator. Do not edit the marker. {marker(cloud_id, tier)}",
                    template_job=cfg.template_job)
        return spec

    def order_key(key: str):
        cid, tier = key.split("|")
        return (cloud_name_of(cid).lower(), -retention(tier), tier)

    for key in sorted(pending, key=order_key):
        vm_list = pending[key]
        if key.split("|")[1] not in cfg.tiers:
            warnings.append(f"Unknown tier in {key}; {len(vm_list)} VMs skipped.")
            continue
        spec = target_spec(key)
        a = {"id": uuid.uuid4().hex[:10], "type": "add_vms" if spec["exists"] else "create_job", "auto": True,
             **spec, "vms": [_vm_ref(v) for v in vm_list],
             "reason": f"{len(vm_list)} VM(s) not yet in a {spec['tier_label']} job"}
        if not spec["exists"] and spec.get("start_time") is None:
            a["blocked"] = f"Backup window is full ({window.capacity} slots of {window.stagger} min)."
        actions.append(a)

    creating = {a["job_key"] for a in actions if a["type"] == "create_job"}
    for (from_job, to_key), vm_list in sorted(moves.items(), key=lambda kv: order_key(kv[0][1])):
        src = jobs_by_id[from_job]
        src_key = src["managed_key"]
        spec = target_spec(to_key)
        cross = src_key.split("|")[0] != to_key.split("|")[0]
        upgrade = retention(to_key.split("|")[1]) > retention(src_key.split("|")[1])
        auto = cfg.retier.auto_cross_cloud if cross else (cfg.retier.auto_upgrade if upgrade else cfg.retier.auto_downgrade)
        a = {"id": uuid.uuid4().hex[:10], "type": "move_vms", "auto": auto, **spec,
             "from_job_id": from_job, "from_job_name": src["name"], "from_job_key": src_key,
             "direction": "cross_cloud" if cross else ("upgrade" if upgrade else "downgrade"),
             "remap_backups": cfg.retier.remap_backups,
             # Target job does not exist and nothing else creates it: phase 1 creates it with these VMs.
             "create_target": (not spec["exists"]) and to_key not in creating,
             "vms": [_vm_ref(v) for v in vm_list],
             "reason": (f"{len(vm_list)} VM(s) moved to cloud '{spec['cloud_name']}'" if cross else
                        f"{len(vm_list)} VM(s) now {spec['tier_label']}"
                        + ("; existing restore points move with them" if cfg.retier.remap_backups else ""))}
        if a["create_target"]:
            creating.add(to_key)
        if not auto and not cross and not upgrade:
            a["reason"] += ". Shortens retention, so it needs approval"
        if not spec["exists"] and spec.get("start_time") is None:
            a["blocked"] = "Backup window is full."
        actions.append(a)

    # ---------------- schedule drift on existing managed jobs
    for key, job in managed.items():
        slot = next((s for s, k in alloc.used.items() if k == key), None)
        if slot is None:
            continue
        want = window.slot_time(slot)
        if job.get("start_time") != want or not job.get("schedule_enabled"):
            cloud_id, tier = key.split("|")
            actions.append({
                "id": uuid.uuid4().hex[:10], "type": "set_schedule", "auto": True, "job_key": key,
                "job_id": job["id"], "job_name": job["name"], "cloud_id": cloud_id, "cloud_name": cloud_name_of(cloud_id),
                "tier": tier, "start_time": want, "slot": slot, "vms": [],
                "reason": f"Schedule {job.get('start_time') or 'unset'}"
                          f"{'' if job.get('schedule_enabled') else ' (disabled)'} to {want}"})

    # ---------------- backup copy jobs: one per managed backup job
    if cfg.backup_copy.enabled:
        for key in sorted(set(managed) | creating, key=order_key):
            if key in managed_copies:
                cj = managed_copies[key]
                if cj.get("retention_days") and int(cj["retention_days"]) != cfg.backup_copy.retention_days:
                    warnings.append(f"Copy job '{cj['name']}' keeps {cj['retention_days']} days, config says "
                                    f"{cfg.backup_copy.retention_days}. Not changed automatically.")
                continue
            cloud_id, tier = key.split("|")
            src_name = managed[key]["name"] if key in managed else new_names.get(key)
            if not src_name:
                continue
            target = cfg.copy_repository_for(cloud_name_of(cloud_id))
            src_repo = managed[key].get("repository") if key in managed else cfg.repository_for(cloud_name_of(cloud_id), tier)
            a = {"id": uuid.uuid4().hex[:10], "type": "create_copy", "auto": True, "job_key": key,
                 "cloud_id": cloud_id, "cloud_name": cloud_name_of(cloud_id), "tier": tier,
                 "source_job_id": managed[key]["id"] if key in managed else None, "source_job_name": src_name,
                 "job_id": None, "job_name": make_copy_name(cfg, src_name, cloud_name_of(cloud_id), tier, taken_names),
                 "repository": target, "retention_days": cfg.backup_copy.retention_days,
                 "mode": cfg.backup_copy.mode, "vms": [],
                 "description": f"Managed by HV Backup Orchestrator. Do not edit the marker. {copy_marker(cloud_id, tier)}",
                 "reason": f"Copy of '{src_name}' to {target or '(no target set)'}, {cfg.backup_copy.retention_days} days"}
            if not target:
                a["blocked"] = "backup_copy.target_repository is not set in config."
            elif src_repo and target.lower() == src_repo.lower():
                a["blocked"] = f"Copy target '{target}' is the same repository as the source job."
            actions.append(a)

    # ---------------- timeline and summary
    final_slots = {k: s for s, k in alloc.used.items()}
    timeline = []
    for s in range(window.capacity):
        owner = alloc.used.get(s)
        entry = {"slot": s, "time": window.slot_time(s), "state": "free"}
        if owner:
            cloud_id, tier = owner.split("|")
            job = managed.get(owner)
            entry.update(state="managed" if job else "planned", job_key=owner, tier=tier,
                         cloud_name=cloud_name_of(cloud_id),
                         job_name=job["name"] if job else new_names.get(owner, owner))
        elif s in reserved:
            entry.update(state="reserved", job_name=", ".join(reserved_names.get(s, [])))
        timeline.append(entry)

    counts: dict[str, int] = defaultdict(int)
    tiers_count: dict[str, int] = defaultdict(int)
    for r in vm_status:
        counts[r["status"]] += 1
        if r["status"] not in ("excluded", "out_of_scope", "library"):
            tiers_count[r["tier"]] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rebalance": rebalance,
        "window": {"start": window.start, "end": window.end, "stagger": window.stagger,
                   "capacity": window.capacity, "used": len(alloc.used), "reserved": len(reserved)},
        "summary": {
            "clouds": len(clouds), "vms": len(vms), "jobs": len(jobs), "managed_jobs": len(managed),
            "copy_jobs": len(managed_copies), "foreign_jobs": len(foreign_jobs),
            "unmanaged_hyperv_jobs": len(jobs) - len(managed),
            "status": dict(counts), "tiers": dict(tiers_count), "actions": len(actions),
            "auto_actions": sum(1 for a in actions if a["auto"] and not a.get("blocked")),
        },
        # The only job IDs any apply step may touch. Everything else is read only.
        "owned": {"backup_job_ids": sorted(managed_ids),
                  "copy_job_ids": sorted(c["id"] for c in managed_copies.values()),
                  "foreign_names": sorted(taken_names - {j["name"].lower() for j in managed.values()}
                                          - {c["name"].lower() for c in managed_copies.values()}
                                          - {n.lower() for n in new_names.values()}
                                          - {a["job_name"].lower() for a in actions if a["type"] == "create_copy"})},
        "copy_jobs": {k: {kk: v.get(kk) for kk in ("id", "name", "repository", "retention_days", "mode")}
                      for k, v in managed_copies.items()},
        "actions": actions,
        "vm_status": vm_status,
        "slot_table": final_slots,
        "timeline": timeline,
        "warnings": warnings,
    }
