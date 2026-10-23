"""Web layer: no login of its own (reverse proxy authenticates), same-site posts only."""
import importlib
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HVBO_MOCK", "true")
    monkeypatch.setenv("HVBO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HVBO_CONFIG", str(tmp_path / "none.yaml"))
    import app.main as main
    main = importlib.reload(main)
    with TestClient(main.app) as c:
        c.main = main
        yield c


def test_pages_open_without_login(client):
    for path in ("/", "/plan", "/schedule", "/runs", "/safety", "/settings", "/diagnostics", "/api/status"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "www-authenticate" not in r.headers


def test_cross_site_post_refused(client):
    r = client.post("/actions/run", data={"mode": "apply"}, headers={"Origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_post_through_proxy_host_allowed_and_user_recorded(client):
    r = client.post("/actions/run", data={"mode": "plan"}, follow_redirects=False,
                    headers={"Origin": "https://hvbo.arc.local", "X-Forwarded-Host": "hvbo.arc.local",
                             "X-Forwarded-User": "harley"})
    assert r.status_code == 303
    while client.main.engine.busy:
        time.sleep(0.02)
    assert client.main.store.runs(1)[0]["trigger"] == "manual:harley"


@pytest.fixture()
def mock_client(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text("mock: true\ndefault_repository: SOBR-Tenants\n"
                       "backup_copy:\n  target_repository: SOBR-Copy-7d\n")
    monkeypatch.setenv("HVBO_MOCK", "true")
    monkeypatch.setenv("HVBO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HVBO_CONFIG", str(cfgfile))
    import app.main as main
    main = importlib.reload(main)
    with TestClient(main.app) as c:
        c.main = main
        yield c


def _wait(client):
    while client.main.engine.busy:
        time.sleep(0.02)


def test_apply_one_action_only(mock_client):
    client = mock_client
    eng, store = client.main.engine, client.main.store
    client.post("/actions/run", data={"mode": "plan"})
    _wait(client)
    latest = store.latest_with_plan()
    actions = latest["plan"]["actions"]
    from app.engine import needs_other_action
    pick = next(a for a in actions if not a.get("blocked") and not needs_other_action(a))
    assert len(actions) > 1
    page = client.get("/plan").text
    assert "/actions/apply-one" in page and pick["id"] in page

    r = client.post("/actions/apply-one", data={"run_id": latest["id"], "action_id": pick["id"]},
                    follow_redirects=False)
    assert r.status_code == 303
    _wait(client)
    run = store.run(int(r.headers["location"].rsplit("/", 1)[1]))
    assert run["mode"] == "apply_one"
    assert [x["id"].removesuffix("-mv") for x in run["results"]] in ([pick["id"]], [pick["id"], pick["id"]])
    assert all(x["ok"] for x in run["results"])
    assert "Refreshing the plan" in run["log"]
    # the stored plan is fresh and still has the other work left
    assert run["plan"]["actions"] and pick["id"] not in {a["id"] for a in run["plan"]["actions"]}


def test_apply_one_unknown_or_dependent_action(mock_client):
    client = mock_client
    store = client.main.store
    client.post("/actions/run", data={"mode": "plan"})
    _wait(client)
    latest = store.latest_with_plan()
    r = client.post("/actions/apply-one", data={"run_id": latest["id"], "action_id": "nope"}, follow_redirects=False)
    _wait(client)
    run = store.run(int(r.headers["location"].rsplit("/", 1)[1]))
    assert run["results"] == [] and "not in this plan" in run["log"]
    from app.engine import needs_other_action
    dep = next((a for a in store.latest_with_plan()["plan"]["actions"] if needs_other_action(a) and not a.get("blocked")), None)
    assert dep
    if dep:
        latest = store.latest_with_plan()
        r = client.post("/actions/apply-one", data={"run_id": latest["id"], "action_id": dep["id"]}, follow_redirects=False)
        _wait(client)
        run = store.run(int(r.headers["location"].rsplit("/", 1)[1]))
        assert run["results"] == [] and "Apply that first" in run["log"]
