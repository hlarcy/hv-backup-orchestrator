from app.config import AppConfig, BackupCopyConfig
from app.planner import build_plan, classify, copy_marker, marker
from app.slots import Window


def cfg(copies=False, **kw):
    c = AppConfig(default_repository="REPO", **kw)
    c.backup_copy = BackupCopyConfig(enabled=copies, target_repository="SOBR-COPY")
    return c


def vm(name, cloud="c1", guid=None, gb=100, **kw):
    return {"id": name, "vm_guid": guid or f"00000000-0000-0000-0000-{abs(hash(name)) % 10**12:012d}",
            "name": name, "cloud_id": cloud, "cloud_name": cloud.upper(), "host": "hv01.x", "cluster": "cl01.x",
            "status": "Running", "in_library": False, "max_gb": gb, "used_gb": gb / 2,
            "custom_property": None, "tags": [], **kw}


def job(jid, name, desc="", start=None, vms=(), repo="REPO", containers=()):
    objs = [{"name": v["name"], "kind": "vm", "include": True, "refs": [v["vm_guid"]]} for v in vms]
    objs += [{"name": c, "kind": "container", "include": True, "refs": []} for c in containers]
    return {"id": jid, "name": name, "description": desc, "type": "Hyper-V Backup", "schedule_enabled": bool(start),
            "start_time": start, "repository": repo, "objects": objs}


CLOUDS = [{"id": "c1", "name": "C1"}, {"id": "c2", "name": "C2"}]


def test_window_capacity_and_times():
    w = Window("18:00", "00:00", 3)
    assert w.capacity == 120
    assert (w.slot_time(0), w.slot_time(1), w.slot_time(119)) == ("18:00", "18:03", "23:57")
    assert w.slot_for_time("18:06") == 2 and w.slot_for_time("18:07") is None
    assert w.slot_for_time("00:00") is None and w.slot_covering("18:07") == 2


def test_retention_is_exactly_the_number_else_default():
    c = cfg()
    assert classify(vm("a"), c)[0] == "d30"                                   # not set
    assert classify(vm("a", custom_property=""), c)[0] == "d30"
    assert classify(vm("a", custom_property="180"), c)[0] == "d180"
    assert classify(vm("a", custom_property="7"), c)[0] == "d7"                # shorter than default: still used
    assert classify(vm("a", custom_property=" 45 "), c)[0] == "d45"
    assert classify(vm("a", custom_property="4000"), c)[0] == "d4000"
    for bad in ("LargeData", "0", "-5", "12.5", "180 days", "abc", "90d"):
        t, why = classify(vm("a", custom_property=bad), c)
        assert t == "d30" and "not a whole number" in why, bad
    assert classify(vm("a", gb=9000), c)[0] == "d30"                           # size never matters
    assert c.tiers["d180"].retention_days == 180 and c.tiers["d7"].label == "7d"


def test_default_retention_is_configurable():
    c = cfg(default_retention_days=14)
    assert classify(vm("a"), c)[0] == "d14" and classify(vm("a", custom_property="x"), c)[0] == "d14"


def test_creates_jobs_per_cloud_staggered():
    vms = [vm("a1"), vm("a2", custom_property="90"), vm("b1", cloud="c2")]
    p = build_plan(cfg(), CLOUDS, vms, [], {})
    creates = [a for a in p["actions"] if a["type"] == "create_job"]
    assert sorted(a["start_time"] for a in creates) == ["18:00", "18:03", "18:06"]
    large = next(a for a in creates if a["tier"] == "d90")
    assert large["retention_days"] == 90 and [v["name"] for v in large["vms"]] == ["a2"]
    assert all(a["retention_days"] == 30 for a in creates if a["tier"] == "d30")


def test_new_vm_added_to_existing_managed_job_and_hand_built_coverage_respected():
    v1, v2, v3 = vm("a1"), vm("a2"), vm("a3")
    jobs = [job("j1", "Hand built", start="18:00", vms=[v1]),
            job("j2", "HVBO C1 Standard", marker("c1", "d30"), "18:03", [v2])]
    p = build_plan(cfg(), CLOUDS, [v1, v2, v3], jobs, {})
    assert [a["type"] for a in p["actions"]] == ["add_vms"]
    assert p["actions"][0]["job_id"] == "j2" and [v["name"] for v in p["actions"][0]["vms"]] == ["a3"]
    assert p["timeline"][0]["state"] == "reserved" and p["slot_table"]["c1|d30"] == 1


def test_managed_only_mode_adds_vms_from_hand_built_jobs():
    v1 = vm("a1")
    p = build_plan(cfg(coverage_mode="managed_only"), CLOUDS, [v1], [job("j1", "Hand built", start="18:00", vms=[v1])], {})
    assert p["actions"][0]["type"] == "create_job"


def test_tagged_vm_in_hand_built_job_still_gets_90_day_job():
    v1 = vm("a1", custom_property="90")
    p = build_plan(cfg(), CLOUDS, [v1], [job("j1", "Hand built", start="18:00", vms=[v1])], {})
    a = p["actions"][0]
    assert a["type"] == "create_job" and a["tier"] == "d90"
    assert "left untouched" in p["vm_status"][0]["note"]


def test_container_coverage_and_exclusion():
    v1 = vm("a1")
    jobs = [job("j1", "Host job", containers=["hv01.x"])]
    assert not build_plan(cfg(), CLOUDS, [v1], jobs, {})["actions"]
    jobs[0]["objects"].append({"name": "a1", "kind": "vm", "include": False, "refs": [v1["vm_guid"]]})
    assert build_plan(cfg(), CLOUDS, [v1], jobs, {})["actions"][0]["type"] == "create_job"


def test_sticky_slots_and_drift():
    jobs = [job("j2", "HVBO C2 Standard", marker("c2", "d30"), "19:00")]
    p = build_plan(cfg(), CLOUDS, [vm("a1")], jobs, {"c2|d30": 0})
    assert next(a for a in p["actions"] if a["type"] == "set_schedule")["start_time"] == "18:00"
    assert next(a for a in p["actions"] if a["type"] == "create_job")["start_time"] == "18:03"


def test_window_full_blocks_creation():
    c = cfg()
    c.schedule.window_start, c.schedule.window_end = "18:00", "18:03"
    p = build_plan(c, CLOUDS, [vm("a1"), vm("b1", cloud="c2")], [], {})
    assert sum(1 for a in p["actions"] if a.get("blocked")) == 1


def test_tag_added_moves_vm_automatically_and_creates_90_day_job():
    v = vm("a1", custom_property="90")
    jobs = [job("j1", "HVBO C1 Standard", marker("c1", "d30"), "18:00", [v])]
    p = build_plan(cfg(), CLOUDS, [v], jobs, {})
    mv = [a for a in p["actions"] if a["type"] == "move_vms"]
    assert len(mv) == 1
    m = mv[0]
    assert m["auto"] and m["direction"] == "upgrade" and m["remap_backups"]
    assert m["from_job_id"] == "j1" and m["tier"] == "d90" and m["create_target"] and m["start_time"] == "18:03"
    assert p["vm_status"][0]["status"] == "retier"


def test_tag_removed_downgrade_needs_approval():
    v = vm("a1")
    jobs = [job("j1", "HVBO C1 Large", marker("c1", "d90"), "18:00", [v])]
    m = build_plan(cfg(), CLOUDS, [v], jobs, {})["actions"][0]
    assert m["type"] == "move_vms" and m["direction"] == "downgrade" and m["auto"] is False


def test_vm_in_both_jobs_keeps_retrying_move():
    v = vm("a1", custom_property="90")
    jobs = [job("j1", "HVBO C1 Standard", marker("c1", "d30"), "18:00", [v]),
            job("j2", "HVBO C1 Large", marker("c1", "d90"), "18:03", [v])]
    m = build_plan(cfg(), CLOUDS, [v], jobs, {})["actions"]
    assert [a["type"] for a in m] == ["move_vms"] and m[0]["job_id"] == "j2" and not m[0]["create_target"]


def test_copy_job_per_managed_job_and_blocked_without_target():
    v = vm("a1")
    jobs = [job("j1", "HVBO C1 Standard", marker("c1", "d30"), "18:00", [v])]
    p = build_plan(cfg(copies=True), CLOUDS, [v, vm("a2", custom_property="90")], jobs, {})
    copies = [a for a in p["actions"] if a["type"] == "create_copy"]
    assert len(copies) == 2 and all(a["retention_days"] == 7 and a["repository"] == "SOBR-COPY" for a in copies)
    assert {a["source_job_name"] for a in copies} >= {"HVBO C1 Standard"}
    # Existing managed copy job: nothing to do.
    cj = [{"id": "cj1", "name": "HVBO C1 Standard Copy", "description": copy_marker("c1", "d30")}]
    p = build_plan(cfg(copies=True), CLOUDS, [v], jobs, {}, copy_jobs=cj)
    assert not [a for a in p["actions"] if a["type"] == "create_copy"]
    # No target configured: blocked, never guessed.
    c = cfg(copies=True)
    c.backup_copy.target_repository = None
    assert build_plan(c, CLOUDS, [v], jobs, {})["actions"][0]["blocked"]
    # Same repository as the source: blocked.
    c = cfg(copies=True)
    c.backup_copy.target_repository = "REPO"
    assert build_plan(c, CLOUDS, [v], jobs, {})["actions"][0]["blocked"]


def test_foreign_jobs_reserve_times_and_names_and_are_never_owned():
    foreign = [{"name": "VMW-Prod-SQL", "type": "VMware Backup", "start_time": "18:00", "schedule_enabled": True},
               {"name": "HVBO C1 30d", "type": "Nutanix AHV Backup", "start_time": None, "schedule_enabled": False}]
    p = build_plan(cfg(), CLOUDS, [vm("a1")], [], {}, foreign_jobs=foreign,
                   all_job_names=["VMW-Prod-SQL", "HVBO C1 30d"])
    a = p["actions"][0]
    assert a["start_time"] == "18:03"                       # 18:00 is the VMware job's
    assert a["job_name"] == "HVBO C1 30d 2"        # never reuses a foreign name
    assert "vmw-prod-sql" in p["owned"]["foreign_names"] and p["owned"]["backup_job_ids"] == []


def test_marker_on_wrong_job_type_does_not_make_it_owned_by_planner_inputs():
    # Only Hyper-V jobs are passed as `jobs`; anything else is foreign and never in owned ids.
    p = build_plan(cfg(), CLOUDS, [], [], {}, foreign_jobs=[
        {"name": "VMW tricky", "type": "VMware Backup", "description": marker("c1", "d30")}])
    assert p["owned"]["backup_job_ids"] == []


def test_duplicate_vm_names_across_tenants_match_by_guid():
    a, b = vm("DC01", cloud="c1"), vm("DC01", cloud="c2", guid="11111111-1111-1111-1111-111111111111")
    p = build_plan(cfg(), CLOUDS, [a, b], [job("j1", "x", vms=[a])], {})
    assert [act["cloud_id"] for act in p["actions"]] == ["c2"]


def test_rebalance_large_first():
    c = cfg()
    c.schedule.order = "large_first"
    jobs = [job("s", "S", marker("c1", "d30"), "18:00"), job("l", "L", marker("c2", "d90"), "20:00")]
    assert build_plan(c, CLOUDS, [], jobs, {}, rebalance=True)["slot_table"] == {"c2|d90": 0, "c1|d30": 1}


# ---------------------------------------------------------------- any number gets its own job

def test_180_day_vm_gets_its_own_job_and_moves_with_backups():
    v = vm("a1", custom_property="180")
    jobs = [job("j1", "HVBO C1 30d", marker("c1", "d30"), "18:00", [v])]
    a = build_plan(cfg(), CLOUDS, [v], jobs, {})["actions"][0]
    assert a["type"] == "move_vms" and a["tier"] == "d180" and a["retention_days"] == 180
    assert a["auto"] and a["direction"] == "upgrade" and a["create_target"]
    assert a["job_name"] == "HVBO C1 180d" and a["description"].endswith("[hvbo cloud=c1 tier=d180]")


def test_shorter_than_default_creates_short_job():
    v = vm("a1", custom_property="7")
    a = build_plan(cfg(), CLOUDS, [v], [], {})["actions"][0]
    assert a["type"] == "create_job" and a["retention_days"] == 7 and a["job_name"] == "HVBO C1 7d"


def test_lowering_retention_needs_approval_unless_configured():
    v = vm("a1", custom_property="90")
    jobs = [job("j1", "HVBO C1 180d", marker("c1", "d180"), "18:00", [v])]
    a = build_plan(cfg(), CLOUDS, [v], jobs, {})["actions"][0]
    assert a["type"] == "move_vms" and a["direction"] == "downgrade" and a["auto"] is False
    c = cfg()
    c.retier.auto_downgrade = True
    assert build_plan(c, CLOUDS, [v], jobs, {})["actions"][0]["auto"] is True


def test_legacy_markers_map_to_days():
    v = vm("a1", custom_property="90")
    jobs = [job("j1", "Old large", marker("c1", "large"), "18:00", [v]),
            job("j2", "Old std", marker("c1", "standard"), "18:03", [vm("a2")])]
    p = build_plan(cfg(), CLOUDS, [v, vm("a2")], jobs, {})
    assert not [a for a in p["actions"] if a["type"] in ("move_vms", "create_job", "add_vms")]
    assert set(p["slot_table"]) == {"c1|d90", "c1|d30"}


def test_repository_per_retention():
    c = cfg(retention_repositories={365: "SOBR-LTR"})
    a = build_plan(c, CLOUDS, [vm("a1", custom_property="365"), vm("a2")], [], {})["actions"]
    repos = {x["retention_days"]: x["repository"] for x in a if x["type"] == "create_job"}
    assert repos == {365: "SOBR-LTR", 30: "REPO"}


def test_rebalance_longest_first():
    c = cfg()
    c.schedule.order = "longest_first"
    jobs = [job("s", "S", marker("c1", "d30"), "18:00"), job("l", "L", marker("c2", "d90"), "20:00"),
            job("y", "Y", marker("c1", "d365"), "21:00")]
    assert build_plan(c, CLOUDS, [], jobs, {}, rebalance=True)["slot_table"] == {"c1|d365": 0, "c2|d90": 1, "c1|d30": 2}
