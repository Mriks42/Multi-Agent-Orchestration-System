"""In-process job store for report runs.

A report takes ~25 seconds, so the HTTP request cannot wait for it. Submitting
returns a job id; the page polls for progress. That is the same submit-and-poll
shape the distributed broker already uses, one layer up.

Deliberately in-memory: jobs are lost on restart and do not span processes. For
a single-instance demo that is the honest trade-off, and `SqliteBroker` is the
upgrade path if it ever needs to outlive a process.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

log = logging.getLogger(__name__)

Status = Literal["queued", "running", "done", "failed"]

AGENT_LABELS = {
    "research": "Research Agent",
    "planning": "Planning Agent",
    "assemble": "Writer Agent",
    "reviewer": "Reviewer Agent",
}


@dataclass
class Step:
    """One agent finishing, as the page shows it."""

    agent: str
    detail: str
    seconds: float = 0.0
    parallel: list[str] = field(default_factory=list)
    """Sections drafted concurrently in the wave this step completed.

    The page's whole subject is orchestration, and a flat list of steps hides
    it: a reader cannot tell a fan-out from a sequence. These are the branches
    that were in flight at once, so the parallelism is shown rather than
    claimed.
    """

    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Job:
    company: str
    quarter: str
    focus: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Status = "queued"
    steps: list[Step] = field(default_factory=list)
    report: str = ""
    error: str = ""
    sourced: int = 0
    total_findings: int = 0
    approved: bool = False
    open_issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "company": self.company,
            "quarter": self.quarter,
            "status": self.status,
            "steps": [
                {
                    "agent": s.agent,
                    "detail": s.detail,
                    "seconds": s.seconds,
                    "parallel": s.parallel,
                }
                for s in self.steps
            ],
            "report": self.report,
            "error": self.error,
            "provenance": {
                "sourced": self.sourced,
                "total": self.total_findings,
                # The number a reader should see before trusting any figure.
                "unsourced": max(self.total_findings - self.sourced, 0),
            },
            "approved": self.approved,
            "open_issues": self.open_issues,
        }


class JobStore:
    """Thread-safe map of job id to Job."""

    def __init__(self, max_jobs: int = 200):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.max_jobs = max_jobs

    def create(self, company: str, quarter: str, focus: str = "") -> Job:
        job = Job(company=company, quarter=quarter, focus=focus)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            # Bounded, so a long-running server cannot grow without limit.
            while len(self._order) > self.max_jobs:
                self._jobs.pop(self._order.pop(0), None)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [self._jobs[i] for i in ids if i in self._jobs]

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def add_step(
        self, job_id: str, agent: str, detail: str,
        seconds: float = 0.0, parallel: list[str] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.steps.append(
                    Step(agent=agent, detail=detail, seconds=seconds,
                         parallel=list(parallel or []))
                )


def run_job(store: JobStore, job: Job, deps, max_revisions: int | None = None) -> None:
    """Execute one report, recording progress as each agent finishes.

    Runs on a worker thread. Every failure is recorded on the job rather than
    raised, because nothing is waiting to catch it.
    """
    from ..graph import run_report

    store.update(job.id, status="running")

    # `write_section` fires once per parallel branch and carries no trace line.
    # Collecting the headings lets the wave that follows show what ran at once.
    wave: list[str] = []
    last = time.monotonic()

    def on_event(node: str, update: dict):
        nonlocal last
        if node == "write_section":
            wave.extend(update.get("sections", {}))
            return

        label = AGENT_LABELS.get(node)
        if not label:
            return

        now = time.monotonic()
        for line in update.get("trace", []):
            store.add_step(
                job.id, label, line.split(":", 1)[-1].strip(),
                seconds=round(now - last, 1), parallel=list(wave),
            )
            wave.clear()  # only the first line of a wave owns its branches
        last = now

    try:
        state = run_report(
            deps,
            company=job.company,
            quarter=job.quarter,
            focus=job.focus,
            max_revisions=max_revisions,
            on_event=on_event,
        )
    except Exception as exc:
        log.exception("job %s failed", job.id)
        store.update(job.id, status="failed", error=f"{type(exc).__name__}: {exc}")
        return

    findings = state.get("findings", []) or []
    review = state.get("review")
    store.update(
        job.id,
        status="done",
        report=state.get("draft", ""),
        sourced=sum(1 for f in findings if f.source_ids),
        total_findings=len(findings),
        approved=bool(review and review.approved),
        open_issues=[
            f"[{i.severity}] {i.section or 'report'}: {i.problem}"
            for i in (review.issues if review and not review.approved else [])
        ],
    )
