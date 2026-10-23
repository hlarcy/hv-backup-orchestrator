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
