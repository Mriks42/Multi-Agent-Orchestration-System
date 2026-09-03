"""The worker process: claims section-writing tasks and returns the prose.

A worker knows nothing about reports, outlines or the graph. It pulls a task,
runs the Writer Agent on it, and pushes back a result -- which is what lets you
run as many as you like, on as many machines as you like.

The lease is renewed while an LLM call is in flight, because a call slower than
the lease would otherwise look like a dead worker and get its task stolen.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from typing import Any

from ..deps import Deps
from ..state import Issue
from .broker import Broker, Task

log = logging.getLogger(__name__)


def default_worker_id() -> str:
    """Identify a worker by host and pid, so logs point at a real process."""
    return f"{socket.gethostname()}-{os.getpid()}"


class _LeaseRenewer:
    """Keeps a claimed task's lease alive while the LLM call runs."""

    def __init__(self, broker: Broker, task_id: str, lease_seconds: float):
        self.broker = broker
        self.task_id = task_id
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        renew = getattr(self.broker, "renew", None)
        if renew is None:
            return self  # backend without renewal; the lease must simply be generous

        def loop():
            while not self._stop.wait(self.lease_seconds / 3):
                try:
                    renew(self.task_id, self.lease_seconds)
                except Exception as exc:  # a failed renewal is not fatal to the work
                    log.debug("lease renewal failed for %s: %s", self.task_id, exc)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        return False


def run_task(deps: Deps, task: Task) -> dict[str, Any]:
    """Execute one section-writing task. Pure function of its payload."""
    from ..agents.writer import make_write_section_node

    payload = dict(task.payload)
    # Issues cross the wire as plain dicts; the Writer expects Issue objects.
    payload["issues"] = [
        i if isinstance(i, Issue) else Issue(**i) for i in payload.get("issues", [])
    ]
    return make_write_section_node(deps)(payload)


class Worker:
    """A polling loop over the broker. One per process."""

    def __init__(
        self,
        broker: Broker,
        deps: Deps,
        worker_id: str | None = None,
        lease_seconds: float = 90.0,
        idle_sleep: float = 0.5,
    ):
        self.broker = broker
        self.deps = deps
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        self.idle_sleep = idle_sleep
        self.completed = 0
        self.failed = 0
        self._stopping = False

    def stop(self, *_) -> None:
        """Ask the loop to finish the current task and exit."""
        log.info("worker %s stopping after current task", self.worker_id)
        self._stopping = True

    def run_once(self) -> bool:
        """Claim and run at most one task. True if work was done."""
        self.broker.reclaim_expired()
        task = self.broker.claim(self.worker_id, self.lease_seconds)
        if task is None:
            return False

        heading = task.payload.get("heading", "?")
        log.info("worker %s claimed %r (attempt %d)", self.worker_id, heading, task.attempts)
        try:
            with _LeaseRenewer(self.broker, task.task_id, self.lease_seconds):
                result = run_task(self.deps, task)
        except Exception as exc:
            # Requeued if attempts remain, so a transient API error costs a
            # retry rather than the whole report.
            log.exception("worker %s failed %r", self.worker_id, heading)
            self.broker.fail(task.task_id, f"{type(exc).__name__}: {exc}")
            self.failed += 1
            return True

        self.broker.complete(task.task_id, result)
        self.completed += 1
        log.info("worker %s completed %r", self.worker_id, heading)
        return True

    def run_forever(self, max_tasks: int | None = None) -> None:
        """Poll until stopped, or until `max_tasks` have been handled."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):  # not the main thread
                pass

        log.info("worker %s online", self.worker_id)
        while not self._stopping:
            if max_tasks is not None and self.completed + self.failed >= max_tasks:
                break
            if not self.run_once():
                time.sleep(self.idle_sleep)
        log.info(
            "worker %s offline (%d completed, %d failed)",
            self.worker_id, self.completed, self.failed,
        )
