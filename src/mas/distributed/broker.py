"""The task-queue contract shared by every broker backend.

Distributing work means accepting that a worker can die holding a task. The
protocol is built around that: a claim is a *lease*, not a handover. A worker
that stops renewing loses the task, and it returns to the queue for someone
else. Everything else here follows from that one decision.

Task lifecycle:

    PENDING ──claim()──▶ RUNNING ──complete()──▶ DONE
       ▲                    │
       │                    ├──fail()──▶ PENDING (attempts < max) or FAILED
       └──lease expiry──────┘
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    """One unit of distributable work: writing a single report section."""

    run_id: str
    payload: dict[str, Any]
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: TaskState = TaskState.PENDING
    attempts: int = 0
    max_attempts: int = 3
    lease_until: float = 0.0
    worker_id: str = ""
    result: Any = None
    error: str = ""

    @property
    def lease_expired(self) -> bool:
        return self.state is TaskState.RUNNING and time.time() > self.lease_until

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts


class BrokerError(RuntimeError):
    """Raised when a run cannot be completed by any worker."""


class Broker(Protocol):
    """A queue that survives its workers.

    Implementations must be safe to call from several processes at once; the
    guarantees below are what the orchestrator and workers rely on.
    """

    def submit(self, run_id: str, payloads: list[dict]) -> list[str]:
        """Enqueue payloads as PENDING tasks. Returns their task ids."""
        ...

    def claim(self, worker_id: str, lease_seconds: float = 60.0) -> Task | None:
        """Atomically take one PENDING task and lease it. None if queue is empty.

        Atomicity is the whole game: two workers must never claim the same task.
        """
        ...

    def complete(self, task_id: str, result: Any) -> None:
        """Record a result. Must be idempotent -- a retried task may finish twice."""
        ...

    def fail(self, task_id: str, error: str) -> None:
        """Record a failure, requeueing the task if attempts remain."""
        ...

    def reclaim_expired(self) -> int:
        """Requeue tasks whose lease lapsed. Returns how many were reclaimed.

        This is what turns a dead worker into a delay instead of a lost report.
        """
        ...

    def gather(self, run_id: str, timeout: float = 300.0) -> dict[str, Any]:
        """Block until every task for `run_id` finishes; return {task_id: result}.

        Raises BrokerError if the run cannot finish -- a task exhausted its
        retries, or the timeout elapsed.
        """
        ...

    def close(self) -> None:
        """Release any connection or file handle."""
        ...
