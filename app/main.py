"""Web UI and API."""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_config
from .db import Store
from .engine import Engine

logging.basicConfig(level=os.environ.get("HVBO_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hvbo")

BASE = Path(__file__).parent
cfg = load_config()
store = Store(cfg.data_dir)
engine = Engine(cfg, store)
scheduler = BackgroundScheduler(timezone=os.environ.get("TZ", "Europe/London"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(engine.scheduled_tick, "interval", minutes=cfg.sync_interval_minutes, id="sync",
                      max_instances=1, coalesce=True)
    scheduler.start()
    log.info("HV Backup Orchestrator started (backend=%s, interval=%s min, auto_apply=%s)",
             engine.backend.name, cfg.sync_interval_minutes, engine.auto_apply)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="HV Backup Orchestrator", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
# No authentication here: the app is published only through a reverse proxy that authenticates users.
# The proxy's user header, if present, is recorded against manual runs.
USER_HEADERS = ("x-forwarded-user", "x-remote-user", "remote-user", "x-auth-request-user",
                "x-forwarded-preferred-username", "x-forwarded-email", "x-auth-request-email")


def who(request: Request) -> str:
    """Caller as reported by the reverse proxy (for the run history), and a same-site check on posts.

    The proxy's session is usually a cookie, so a page on another site could otherwise make a
    signed-in browser submit 'apply'. Posts must come from this site's own pages.
    """
    if request.method == "POST":
        src = request.headers.get("origin") or request.headers.get("referer")
        if src:
            this_site = {h.strip().lower() for h in (request.headers.get("x-forwarded-host") or "").split(",") if h.strip()}
            this_site.add((request.headers.get("host") or "").lower())
            if urlparse(src).netloc.lower() not in this_site:
                raise HTTPException(403, "Cross-site request refused")
    for h in USER_HEADERS:
        if request.headers.get(h):
            return request.headers[h][:120]
    return "web"


def next_run() -> str | None:
    job = scheduler.get_job("sync")
    return job.next_run_time.strftime("%H:%M") if job and job.next_run_time else None


def ctx(request: Request, **kw) -> dict:
    latest = store.latest_with_plan()
    return {
        "request": request, "cfg": cfg, "engine": engine, "backend": engine.backend.name,
        "auto_apply": engine.auto_apply, "paused": engine.paused, "busy": engine.busy,
        "current_run": engine.current_run, "next_run": next_run(),
        "latest": latest, "plan": latest["plan"] if latest else None, **kw,
    }


STATUS_LABEL = {
    "protected": "Protected", "pending": "To be added", "excluded": "Excluded", "library": "In library",
    "out_of_scope": "Out of scope", "retier": "Moving tier", "wrong_cloud": "Moving cloud",
    "skipped_stopped": "Stopped (skipped)", "no_guid": "No VM GUID",
}
templates.env.globals["STATUS_LABEL"] = STATUS_LABEL


def cloud_rows(plan: dict) -> list[dict]:
    rows: dict[str, dict] = {}
    for c in plan["inventory"]["clouds"]:
        rows[c["id"]] = {"id": c["id"], "name": c["name"], "vms": 0, "tiers": {},
                         "excluded": 0, "pending": 0, "protected": 0, "moving": 0, "jobs": {}, "copies": {}}
    for v in plan["vm_status"]:
        r = rows.get(v["cloud_id"])
        if not r:
            continue
        r["vms"] += 1
        if v["status"] == "excluded":
            r["excluded"] += 1
        elif v["tier"] in cfg.tiers:
            r["tiers"][v["tier"]] = r["tiers"].get(v["tier"], 0) + 1
        if v["status"] == "pending":
            r["pending"] += 1
        if v["status"] == "protected":
            r["protected"] += 1
        if v["status"] in ("retier", "wrong_cloud"):
            r["moving"] += 1
    for j in plan["inventory"]["jobs"]:
        if j.get("managed_key"):
            cid, tier = j["managed_key"].split("|")
            if cid in rows:
                rows[cid]["jobs"][tier] = j
    for key, cj in (plan.get("copy_jobs") or {}).items():
        cid, tier = key.split("|")
        if cid in rows:
            rows[cid]["copies"][tier] = cj
    for a in plan["actions"]:
        if a["cloud_id"] not in rows:
            continue
        if a["type"] == "create_job" or (a["type"] == "move_vms" and a.get("create_target")):
            rows[a["cloud_id"]]["jobs"].setdefault(a["tier"], {"name": a["job_name"], "start_time": a["start_time"],
                                                              "planned": True, "vm_count": len(a["vms"]),
                                                              "retention_days": a["retention_days"],
                                                              "repository": a.get("repository")})
        elif a["type"] == "create_copy":
            rows[a["cloud_id"]]["copies"].setdefault(a["tier"], {"name": a["job_name"], "planned": True,
                                                                "repository": a["repository"],
                                                                "retention_days": a["retention_days"],
                                                                "blocked": a.get("blocked")})
    for r in rows.values():
        r["tier_keys"] = cfg.tiers_by_retention(set(r["tiers"]) | set(r["jobs"]))
    return sorted(rows.values(), key=lambda r: r["name"].lower())


# --------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(who)):
    c = ctx(request)
    return templates.TemplateResponse(request, "dashboard.html", {
        **c, "runs": store.runs(8), "clouds": cloud_rows(c["plan"]) if c["plan"] else []})


@app.get("/clouds/{cloud_id}", response_class=HTMLResponse)
def cloud_detail(cloud_id: str, request: Request, user: str = Depends(who)):
    c = ctx(request)
    if not c["plan"]:
        return RedirectResponse("/", 303)
    cloud = next((r for r in cloud_rows(c["plan"]) if r["id"] == cloud_id), None)
    if not cloud:
        raise HTTPException(404)
    vms = [v for v in c["plan"]["vm_status"] if v["cloud_id"] == cloud_id]
    actions = [a for a in c["plan"]["actions"] if a.get("cloud_id") == cloud_id]
    return templates.TemplateResponse(request, "cloud.html", {**c, "cloud": cloud, "vms": vms, "actions": actions})


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, user: str = Depends(who)):
    return templates.TemplateResponse(request, "plan.html", ctx(request))


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, user: str = Depends(who)):
    c = ctx(request)
    hours = defaultdict(list)
    for s in (c["plan"] or {}).get("timeline", []):
        hours[s["time"][:2]].append(s)
    return templates.TemplateResponse(request, "schedule.html", {**c, "hours": dict(hours)})


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, user: str = Depends(who)):
    return templates.TemplateResponse(request, "runs.html", {**ctx(request), "runs": store.runs(200)})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(run_id: int, request: Request, user: str = Depends(who)):
    run = store.run(run_id)
    if not run:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "run.html", {**ctx(request), "run": run})


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request, user: str = Depends(who)):
    return templates.TemplateResponse(request, "diagnostics.html", {**ctx(request), "probe": store.get("last_probe")})


@app.get("/safety", response_class=HTMLResponse)
def safety_page(request: Request, user: str = Depends(who)):
    c = ctx(request)
    inv = (c["plan"] or {}).get("inventory", {})
    hand_built = [j for j in inv.get("jobs", []) if not j.get("managed_key")]
    return templates.TemplateResponse(request, "safety.html", {
        **c, "foreign": inv.get("foreign_jobs", []), "hand_built": hand_built})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(who)):
    import yaml
    return templates.TemplateResponse(request, "settings.html", {
        **ctx(request), "config_yaml": yaml.safe_dump(cfg.redacted(), sort_keys=False)})


# --------------------------------------------------------------------------- actions

@app.post("/actions/run")
def action_run(mode: str = Form(...), user: str = Depends(who)):
    if mode not in ("plan", "apply", "rebalance"):
        raise HTTPException(400)
    run_id = engine.start(f"manual:{user}", "plan" if mode == "rebalance" else mode, rebalance=(mode == "rebalance"))
    return RedirectResponse(f"/runs/{run_id}" if run_id else "/?busy=1", 303)


@app.post("/actions/apply-plan")
def action_apply_plan(run_id: int = Form(...), include_manual: bool = Form(False), user: str = Depends(who)):
    new_id = engine.start(f"manual:{user}", "apply_stored", from_run=run_id, include_manual=include_manual)
    return RedirectResponse(f"/runs/{new_id}" if new_id else "/?busy=1", 303)


@app.post("/actions/toggle")
def action_toggle(key: str = Form(...), user: str = Depends(who)):
    if key == "auto_apply":
        store.set("auto_apply", not engine.auto_apply)
    elif key == "paused":
        store.set("paused", not engine.paused)
    else:
        raise HTTPException(400)
    log.info("%s toggled %s", user, key)
    return RedirectResponse("/", 303)


@app.post("/actions/probe")
def action_probe(user: str = Depends(who)):
    from .db import now
    store.set("last_probe", {"at": now(), "result": engine.backend.probe()})
    return RedirectResponse("/diagnostics", 303)


# --------------------------------------------------------------------------- JSON API

@app.get("/api/status")
def api_status(user: str = Depends(who)):
    latest = store.latest_with_plan()
    return {"busy": engine.busy, "current_run": engine.current_run, "auto_apply": engine.auto_apply,
            "paused": engine.paused, "next_run": next_run(), "backend": engine.backend.name,
            "latest_summary": latest["summary"] if latest else None}


@app.get("/api/runs/{run_id}")
def api_run(run_id: int, user: str = Depends(who)):
    run = store.run(run_id)
    if not run:
        raise HTTPException(404)
    return JSONResponse({k: run[k] for k in ("id", "status", "started", "finished", "summary", "error")})


@app.get("/api/plan")
def api_plan(user: str = Depends(who)):
    latest = store.latest_with_plan()
    return latest["plan"] if latest else {}


@app.get("/healthz")
def healthz():
    return {"ok": True}
