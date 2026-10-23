"""Tiny SQLite store: run history, sticky slot table, last inventory snapshot, runtime settings."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT NOT NULL, finished TEXT,
  trigger TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
  summary TEXT, plan TEXT, results TEXT, log TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS slots (
  job_key TEXT PRIMARY KEY, slot INTEGER NOT NULL, job_name TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(Path(data_dir) / "hvbo.db", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---- kv
    def get(self, k: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, k: str, v: Any) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self.conn.commit()

    # ---- slots
    def slot_table(self) -> dict[str, int]:
        with self.lock:
            return {r["job_key"]: r["slot"] for r in self.conn.execute("SELECT job_key, slot FROM slots")}

    def save_slots(self, table: dict[str, int], names: dict[str, str] | None = None, replace: bool = False) -> None:
        names = names or {}
        with self.lock:
            if replace:
                self.conn.execute("DELETE FROM slots")
            for k, s in table.items():
                self.conn.execute("INSERT OR REPLACE INTO slots(job_key, slot, job_name, updated) VALUES(?,?,?,?)",
                                  (k, s, names.get(k), now()))
            self.conn.commit()

    # ---- runs
    def start_run(self, trigger: str, mode: str) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO runs(started, trigger, mode, status) VALUES(?,?,?,?)",
                                    (now(), trigger, mode, "running"))
            self.conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None:
        cols = {k: (json.dumps(v) if k in ("summary", "plan", "results") else v) for k, v in fields.items()}
        cols.update(status=status, finished=now())
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.lock:
            self.conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*cols.values(), run_id))
            self.conn.commit()

    def update_run(self, run_id: int, **fields: Any) -> None:
        cols = {k: (json.dumps(v) if k in ("summary", "plan", "results") else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.lock:
            self.conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*cols.values(), run_id))
            self.conn.commit()

    def _row(self, r: sqlite3.Row | None) -> dict | None:
        if not r:
            return None
        d = dict(r)
        for k in ("summary", "plan", "results"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def run(self, run_id: int) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def runs(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, started, finished, trigger, mode, status, summary, error FROM runs ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def latest_with_plan(self) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute(
                "SELECT * FROM runs WHERE plan IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone())

    def prune(self, keep: int = 500) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (keep,))
            self.conn.commit()
