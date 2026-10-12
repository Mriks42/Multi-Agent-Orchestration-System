"""An append-only record of what an agent asked this server to do.

An MCP server hands a tool that spends money to something that decides for
itself when to call it. The web UI at least has a person behind every click;
here the caller is a model, so the only account of what happened is the one
this file keeps.

Two properties it has to have:

* **Denials are recorded too.** A log of successful calls answers "what did it
  do"; it does not answer "what did it try". An agent repeatedly probing
  `run_report` without a code is the thing worth seeing, and it never appears
  in a success-only log.
* **Writing never blocks the tool.** Records go onto a queue and a daemon
  thread drains it, so a slow or full disk costs a tool call nothing. The queue
  is bounded: under pressure the log drops records and says so, rather than
  growing until the process dies.

Secrets never reach it. `sanitise` masks the access code by name, and nothing
here ever receives the API key.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PATH = "mas-mcp-audit.jsonl"

#: Argument names whose values must never be written, in any casing.
SECRET_ARGS = frozenset({"access_code", "code", "api_key", "openai_api_key", "token", "password"})

MASK = "***"


def sanitise(args: dict[str, Any] | None) -> dict[str, Any]:
    """Arguments with every secret replaced by a mask.

    Masks by *name* rather than by value: a value-based filter cannot know the
    code when the supplied one is wrong, and a wrong guess is exactly the
    record worth keeping. The key fact -- that a code was supplied at all -- is
    preserved, because "called with no code" and "called with a bad code" are
    different events.
    """
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key.lower() in SECRET_ARGS:
            out[key] = MASK if value else None
        else:
            out[key] = value
    return out


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class AuditLog:
    """JSON Lines, one record per tool call, written off the calling thread."""

    def __init__(self, path: str | Path | None = None, max_pending: int = 1000):
        self.path = Path(path or os.getenv("MAS_MCP_AUDIT") or DEFAULT_PATH)
        self._queue: queue.Queue = queue.Queue(maxsize=max_pending)
        self._dropped = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True, name="mas-mcp-audit")
        self._thread.start()

    # ----------------------------------------------------------------- writing

    def record(
        self,
        tool: str,
        *,
        request_id: str,
        access_mode: str,
        allowed: bool,
        outcome: str,
        args: dict[str, Any] | None = None,
        reason: str = "",
        job_id: str = "",
        cost: dict | None = None,
        provenance: dict | None = None,
    ) -> dict:
        """Queue one record. Returns it, so a caller can assert on it in a test."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "tool": tool,
            "args": sanitise(args),
            "access_mode": access_mode,
            "allowed": allowed,
            "outcome": outcome,
        }
        if reason:
            entry["reason"] = reason
        if job_id:
            entry["job_id"] = job_id
        if cost:
            entry["cost"] = cost
        if provenance:
            entry["provenance"] = provenance

        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            # Losing a record beats blocking a tool call. Count it so the gap
            # is visible rather than silent.
            with self._lock:
                self._dropped += 1
            log.warning("audit queue full; dropped %d record(s) so far", self._dropped)
        return entry

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait for queued records to reach disk. For tests and shutdown.

        Returns whether the queue drained in time. Polls rather than using
        `Queue.join()`, which cannot time out: a stuck writer must not hang a
        test or a shutdown for ever.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    # ----------------------------------------------------------------- reading

    def tail(self, limit: int = 20) -> list[dict]:
        """The last `limit` records, newest last. Skips any unreadable line."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # --------------------------------------------------------------- internals

    def _drain(self) -> None:
        while True:
            entry = self._queue.get()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
            except OSError as exc:  # pragma: no cover - disk failure
                log.warning("could not write audit record: %s", exc)
            finally:
                self._queue.task_done()
