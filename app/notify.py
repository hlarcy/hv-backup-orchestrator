"""Email alerts over SMTP.

Three kinds of alert:
  change     a run created or changed jobs in VBR (sent every time; failures while applying included)
  attention  actions waiting for approval or blocked (sent when the list changes, then as a reminder)
  failure    a run failed outright, e.g. SCVMM or VBR unreachable (sent when the error changes, then as a reminder)
"""
from __future__ import annotations

import hashlib
import json
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from .config import AppConfig
from .db import Store, now

log = logging.getLogger("hvbo.notify")

ACTION_LABEL = {
    "create_job": "Create job", "add_vms": "Add VMs", "move_vms": "Move VMs (retention change)",
    "set_schedule": "Fix schedule", "create_copy": "Create copy job", "rename_job": "Rename job (cloud renamed)",
    "remove_deleted": "Remove deleted VMs", "set_gfs": "Set weekly GFS / synthetic full", "set_encryption": "Turn on backup encryption",
}
OP_LABEL = {
    "ensure_job": "Job", "set_schedule": "Schedule", "retier_vm": "Retention move", "ensure_copy": "Copy job",
    "rename_job": "Rename", "remove_deleted_vms": "Deleted VMs", "set_gfs": "Weekly GFS", "set_encryption": "Encryption",
}


class Notifier:
    def __init__(self, cfg: AppConfig, store: Store):
        self.cfg = cfg
        self.e = cfg.email
        self.store = store

    # ------------------------------------------------------------------ public
    @property
    def configured(self) -> bool:
        return bool(self.e.enabled and self.e.host and self.e.sender and self.e.to)

    def after_run(self, run_id: int, trigger: str, mode: str, plan: dict, results: list[dict],
                  include_manual: bool = False) -> None:
        try:
            if results and self.e.on_change:
                self._send_changes(run_id, trigger, results)
            if self.e.on_attention:
                self._send_attention(run_id, mode, plan, results, include_manual)
            self.store.set("email_failure_state", None)   # a run completed, so any outage is over
        except Exception:  # noqa: BLE001  never let email break a run
            log.exception("Email alert failed")

    def run_failed(self, run_id: int, trigger: str, mode: str, error: str) -> None:
        if not self.e.on_failure:
            return
        try:
            if self._due("email_failure_state", error):
                self._send(f"Run #{run_id} failed", "\n".join([
                    f"Run #{run_id} ({mode}, triggered by {trigger}) failed before it could finish.",
                    "", error, "", "Nothing further was changed in VBR by this run.",
                    self._link(f"/runs/{run_id}")]))
        except Exception:  # noqa: BLE001
            log.exception("Email alert failed")

    def send_test(self) -> dict:
        return self._send("Test message", "This is a test email from the HV Backup Orchestrator.\n"
                          + self._link("/diagnostics"), force=True)

    # ------------------------------------------------------------------ alerts
    def _send_changes(self, run_id: int, trigger: str, results: list[dict]) -> None:
        ok = [r for r in results if r.get("ok")]
        partial = [r for r in results if not r.get("ok") and r.get("partial")]
        failed = [r for r in results if not r.get("ok") and not r.get("partial")]
        subject = f"{len(ok)} change(s) applied"
        if failed:
            subject += f", {len(failed)} FAILED"
        if partial:
            subject += f", {len(partial)} in progress"
        lines = [f"Run #{run_id} (triggered by {trigger}) changed the following in Veeam Backup & Replication.", ""]
        for title, group in (("FAILED", failed), ("In progress, finished on the next sync", partial), ("Done", ok)):
            if not group:
                continue
            lines.append(f"{title}:")
            for r in group:
                msgs = "; ".join(m for m in (r.get("messages") or []) if m)
                lines.append(f"  - {OP_LABEL.get(r.get('op'), r.get('op'))}: {r.get('job_name') or ''}"
                             + (f" ({msgs})" if msgs else ""))
                v = r.get("verify") or {}
                if v:
                    bits = [f"start {v['start_time']}" if v.get("start_time") else "",
                            f"retention {v.get('retention_days')} {v.get('retention_type') or ''}".strip()
                            if v.get("retention_days") is not None else "",
                            f"{v['object_count']} object(s)" if v.get("object_count") is not None else ""]
                    lines.append("      read back from VBR: " + ", ".join(b for b in bits if b))
            lines.append("")
        lines.append(self._link(f"/runs/{run_id}"))
        self._send(subject, "\n".join(lines))

    def _send_attention(self, run_id: int, mode: str, plan: dict, results: list[dict], include_manual: bool) -> None:
        applied = {r["id"].removesuffix("-mv") for r in results if r.get("ok")}
        items = []
        auto_apply = bool(self.store.get("auto_apply", self.cfg.auto_apply))
        for a in plan.get("actions", []):
            if a["id"] in applied:
                continue
            if a.get("blocked"):
                items.append(("Blocked", a, a["blocked"]))
            elif not a["auto"] and not include_manual:
                items.append(("Needs approval", a, a.get("reason", "")))
            elif mode == "plan" and not auto_apply:
                items.append(("Waiting (auto-apply is off)", a, a.get("reason", "")))
        sig = json.dumps(sorted((k, a["type"], a.get("job_key"), a.get("job_name"),
                                 sorted(v.get("vm_guid") or v.get("name") or "" for v in a.get("vms") or []), why)
                                for k, a, why in items))
        if not items:
            self.store.set("email_attention_state", None)
            return
        if not self._due("email_attention_state", sig):
            return
        lines = [f"{len(items)} action(s) need someone to look at them (from run #{run_id}).", ""]
        for kind in ("Blocked", "Needs approval", "Waiting (auto-apply is off)"):
            group = [(a, why) for k, a, why in items if k == kind]
            if not group:
                continue
            lines.append(f"{kind}:")
            for a, why in group:
                vms = ", ".join(v.get("name") or "" for v in a.get("vms") or [])
                target = f" -> {a['new_name']}" if a.get("new_name") else ""
                lines.append(f"  - {ACTION_LABEL.get(a['type'], a['type'])}: {a.get('job_name')}{target}"
                             f" (cloud {a.get('cloud_name') or '?'})")
                if why:
                    lines.append(f"      {why}")
                if vms:
                    lines.append(f"      VMs: {vms}")
            lines.append("")
        lines.append("Review and apply them on the Plan page.")
        lines.append(self._link("/plan"))
        self._send(f"{len(items)} action(s) need attention", "\n".join(lines))

    # ------------------------------------------------------------------ plumbing
    def _due(self, key: str, fingerprint: str) -> bool:
        """True if this alert is new, different from last time, or due a reminder."""
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        state = self.store.get(key) or {}
        due = state.get("digest") != digest
        if not due and self.e.reminder_hours and state.get("sent_at"):
            due = datetime.now(timezone.utc) - datetime.fromisoformat(state["sent_at"]) >= timedelta(hours=self.e.reminder_hours)
        if due:
            self.store.set(key, {"digest": digest, "sent_at": now()})
        return due

    def _link(self, path: str) -> str:
        return f"Open: {self.e.app_url.rstrip('/')}{path}" if self.e.app_url else ""

    def _send(self, subject: str, body: str, force: bool = False) -> dict:
        status = {"at": now(), "subject": subject, "ok": False, "error": None}
        if not self.configured:
            status["error"] = "Email is not enabled or not fully configured (enabled, host, sender, to)."
            if force:
                self.store.set("email_last", status)
            return status
        msg = EmailMessage()
        msg["Subject"] = f"{self.e.subject_prefix} {subject}".strip()
        msg["From"] = self.e.sender
        msg["To"] = ", ".join(self.e.to)
        msg.set_content(body.rstrip() + "\n\n-- \nHV Backup Orchestrator\n")
        try:
            ctx = ssl.create_default_context()
            if self.e.security == "ssl":
                server = smtplib.SMTP_SSL(self.e.host, self.e.port or 465, timeout=self.e.timeout, context=ctx)
            else:
                server = smtplib.SMTP(self.e.host, self.e.port or 587, timeout=self.e.timeout)
            with server:
                if self.e.security == "starttls":
                    server.starttls(context=ctx)
                if self.e.username:
                    server.login(self.e.username, self.e.password or "")
                server.send_message(msg)
            status["ok"] = True
            log.info("Email sent: %s", subject)
        except Exception as exc:  # noqa: BLE001
            status["error"] = f"{type(exc).__name__}: {exc}"
            log.error("Email failed: %s", status["error"])
        self.store.set("email_last", status)
        return status
