"""A broker backed by a single SQLite file.

Chosen so the distributed path runs on a laptop with nothing installed: real
OS processes, real concurrent claims, real lease expiry -- just no server. The
Redis backend is the same contract for deployment.

The two things that make SQLite safe here:

* WAL mode, so readers never block the writer.
* `BEGIN IMMEDIATE` around a claim, which takes the write lock before reading.
  Without it two workers can both read the same PENDING row and both claim it;
  with it the second blocks, then sees the row already RUNNING.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .broker import Broker, BrokerError, Task, TaskState

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_until  REAL NOT NULL DEFAULT 0,
    worker_id    TEXT NOT NULL DEFAULT '',
    result       TEXT,
    error        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_run   ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, created_at);
"""


class SqliteBroker(Broker):
    """Multi-process task queue in one file."""

    def __init__(self, path: str | Path, poll_interval: float = 0.2):
        self.path = str(path)
        self.poll_interval = poll_interval
        self._conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes

    def submit(self, run_id: str, payloads: list[dict]) -> list[str]:
        tasks = [Task(run_id=run_id, payload=p) for p in payloads]
        now = time.time()
        with self._write():
            self._conn.executemany(
                "INSERT INTO tasks (task_id, run_id, payload, state, max_attempts, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (t.task_id, t.run_id, json.dumps(t.payload), TaskState.PENDING.value,
                     t.max_attempts, now)
                    for t in tasks
                ],
            )
        log.info("submitted %d task(s) for run %s", len(tasks), run_id)
        return [t.task_id for t in tasks]

    def claim(self, worker_id: str, lease_seconds: float = 60.0) -> Task | None:
        """Take the oldest PENDING task under the write lock."""
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE state = ? ORDER BY created_at LIMIT 1",
                (TaskState.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE tasks SET state = ?, worker_id = ?, lease_until = ?, attempts = attempts + 1"
                " WHERE task_id = ?",
                (TaskState.RUNNING.value, worker_id, time.time() + lease_seconds, row["task_id"]),
            )
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)
            ).fetchone()
        return self._to_task(row)

    def complete(self, task_id: str, result: Any) -> None:
        # Guarded on state so a late result from a worker whose lease expired
        # cannot overwrite the answer another worker already produced.
        with self._write():
            self._conn.execute(
                "UPDATE tasks SET state = ?, result = ?, error = ''"
                " WHERE task_id = ? AND state != ?",
                (TaskState.DONE.value, json.dumps(result), task_id, TaskState.DONE.value),
            )

    def fail(self, task_id: str, error: str) -> None:
        with self._write():
            row = self._conn.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return
            dead = row["attempts"] >= row["max_attempts"]
            self._conn.execute(
                "UPDATE tasks SET state = ?, error = ?, worker_id = '', lease_until = 0"
                " WHERE task_id = ?",
                (TaskState.FAILED.value if dead else TaskState.PENDING.value, error, task_id),
            )
        log.warning("task %s failed (%s): %s", task_id, "dead" if dead else "requeued", error)

    def renew(self, task_id: str, lease_seconds: float) -> None:
        """Extend a running task's lease. Called while an LLM call is in flight."""
        with self._write():
            self._conn.execute(
                "UPDATE tasks SET lease_until = ? WHERE task_id = ? AND state = ?",
                (time.time() + lease_seconds, task_id, TaskState.RUNNING.value),
            )

    def reclaim_expired(self) -> int:
        """Return tasks from workers that stopped renewing their lease."""
        with self._write():
            cur = self._conn.execute(
                "UPDATE tasks SET state = ?, worker_id = '', lease_until = 0"
                " WHERE state = ? AND lease_until < ? AND attempts < max_attempts",
                (TaskState.PENDING.value, TaskState.RUNNING.value, time.time()),
            )
            reclaimed = cur.rowcount or 0
            # A task whose worker died and which has no attempts left is dead,
            # not merely late -- mark it so `gather` fails fast instead of
            # waiting out the timeout for work nobody will retry.
            self._conn.execute(
                "UPDATE tasks SET state = ?, error = 'lease expired, attempts exhausted'"
                " WHERE state = ? AND lease_until < ? AND attempts >= max_attempts",
                (TaskState.FAILED.value, TaskState.RUNNING.value, time.time()),
            )
        if reclaimed:
            log.warning("reclaimed %d task(s) from expired leases", reclaimed)
        return reclaimed

    # ------------------------------------------------------------------- reads

    def gather(self, run_id: str, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            self.reclaim_expired()
            rows = self._conn.execute(
                "SELECT task_id, state, result, error FROM tasks WHERE run_id = ?", (run_id,)
            ).fetchall()
            if not rows:
                return {}

            dead = [r for r in rows if r["state"] == TaskState.FAILED.value]
            if dead:
                raise BrokerError(
                    f"{len(dead)} task(s) in run {run_id} failed permanently: {dead[0]['error']}"
                )
            if all(r["state"] == TaskState.DONE.value for r in rows):
                return {r["task_id"]: json.loads(r["result"]) for r in rows}

            if time.time() > deadline:
                pending = sum(1 for r in rows if r["state"] != TaskState.DONE.value)
                raise BrokerError(
                    f"timed out after {timeout}s with {pending}/{len(rows)} task(s) unfinished"
                )
            time.sleep(self.poll_interval)

    def stats(self, run_id: str) -> dict[str, int]:
        """Task counts by state, for progress display."""
        rows = self._conn.execute(
            "SELECT state, COUNT(*) n FROM tasks WHERE run_id = ? GROUP BY state", (run_id,)
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def close(self) -> None:
        self._conn.close()

    # --------------------------------------------------------------- internals

    def _write(self):
        """Context manager taking SQLite's write lock up front."""
        return _ImmediateTransaction(self._conn)

    @staticmethod
    def _to_task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            run_id=row["run_id"],
            payload=json.loads(row["payload"]),
            state=TaskState(row["state"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            lease_until=row["lease_until"],
            worker_id=row["worker_id"],
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
        )


class _ImmediateTransaction:
    """BEGIN IMMEDIATE ... COMMIT, so a read-then-write claim stays atomic."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
