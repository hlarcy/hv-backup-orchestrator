"""Email alerts: what is sent, and that repeats are suppressed until something changes."""
import pytest

from app.config import AppConfig, EmailConfig
from app.db import Store
from app.notify import Notifier

SENT = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): self.tls = True
    def login(self, u, p): self.user = u
    def send_message(self, msg): SENT.append(msg)


@pytest.fixture()
def notifier(tmp_path, monkeypatch):
    import app.notify as n
    monkeypatch.setattr(n.smtplib, "SMTP", FakeSMTP)
    SENT.clear()
    c = AppConfig()
    c.email = EmailConfig(enabled=True, host="smtp.arc.local", sender="hvbo@arc.local", to=["ops@arc.local"],
                          app_url="https://hvbo.arc.local")
    return Notifier(c, Store(str(tmp_path)))


def plan(*actions):
    return {"actions": list(actions)}


def act(i, type_="create_job", auto=True, blocked=None, name="HVBO C1 30d"):
    a = {"id": i, "type": type_, "auto": auto, "job_key": "c1|d30", "job_name": name, "cloud_name": "C1",
         "vms": [{"name": "vm1", "vm_guid": "g1"}], "reason": "why"}
    if blocked:
        a["blocked"] = blocked
    return a


def test_change_email_lists_results_and_link(notifier):
    results = [{"id": "1", "op": "ensure_job", "ok": True, "job_name": "HVBO C1 30d", "messages": ["Created job"],
                "verify": {"start_time": "18:00", "retention_days": 30, "retention_type": "Days", "object_count": 1}},
               {"id": "2", "op": "ensure_copy", "ok": False, "job_name": "HVBO C1 30d Copy", "messages": ["boom"]}]
    notifier.after_run(5, "schedule", "apply", plan(), results)
    assert len(SENT) == 1
    m = SENT[0]
    assert m["Subject"] == "[HVBO] 1 change(s) applied, 1 FAILED" and m["To"] == "ops@arc.local"
    body = m.get_content()
    assert "FAILED:" in body and "boom" in body and "start 18:00" in body and "https://hvbo.arc.local/runs/5" in body


def test_attention_sent_once_until_it_changes(notifier):
    p = plan(act("a", "move_vms", auto=False), act("b", blocked="Window full"))
    notifier.after_run(1, "schedule", "plan", p, [])
    notifier.after_run(2, "schedule", "plan", p, [])
    assert len(SENT) == 1 and "need attention" in SENT[0]["Subject"]
    body = SENT[0].get_content()
    assert "Blocked:" in body and "Needs approval:" in body and "Window full" in body
    notifier.after_run(3, "schedule", "plan", plan(act("a", "move_vms", auto=False)), [])
    assert len(SENT) == 2                                     # list changed
    notifier.after_run(4, "schedule", "plan", plan(), [])    # all clear resets
    notifier.after_run(5, "schedule", "plan", plan(act("a", "move_vms", auto=False)), [])
    assert len(SENT) == 3


def test_auto_actions_waiting_only_when_auto_apply_off(notifier):
    notifier.after_run(1, "schedule", "plan", plan(act("a")), [])
    assert len(SENT) == 1 and "auto-apply is off" in SENT[0].get_content()
    SENT.clear()
    notifier.store.set("auto_apply", True)
    notifier.store.set("email_attention_state", None)
    notifier.after_run(2, "schedule", "apply", plan(act("a")), [{"id": "a", "op": "ensure_job", "ok": True, "job_name": "x"}])
    assert [m["Subject"] for m in SENT] == ["[HVBO] 1 change(s) applied"]


def test_failure_deduplicated_and_reset_on_success(notifier):
    notifier.run_failed(1, "schedule", "plan", "SCVMM unreachable")
    notifier.run_failed(2, "schedule", "plan", "SCVMM unreachable")
    assert len(SENT) == 1 and "failed" in SENT[0]["Subject"]
    notifier.after_run(3, "schedule", "plan", plan(), [])
    notifier.run_failed(4, "schedule", "plan", "SCVMM unreachable")
    assert len(SENT) == 2


def test_not_configured_sends_nothing(tmp_path):
    n = Notifier(AppConfig(), Store(str(tmp_path)))
    assert n.send_test()["ok"] is False and "not enabled" in n.send_test()["error"]
