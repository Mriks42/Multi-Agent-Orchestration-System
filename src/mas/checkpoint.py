"""Durable run state, so a crashed orchestrator resumes instead of restarting.

The distributed layer made *tasks* fault-tolerant: kill a worker and another
picks up its section. But the orchestrator held everything else in memory, so
killing it lost the research, the outline and every finished section -- the
expensive parts. This closes that asymmetry.

LangGraph writes a checkpoint after every node. Resuming means invoking the same
graph with the same `thread_id` and no new input: it replays from the last
completed node rather than re-running the pipeline.

A `thread_id` is therefore an identity, not a label. Reusing one for a different
company resumes that company's run.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = "mas-runs.db"


def thread_id(company: str, quarter: str, suffix: str = "") -> str:
    """A stable id for one (company, quarter, day).

    Deterministic on purpose: re-running the same report on the same day resumes
    it rather than silently starting a second one. `suffix` forces a fresh run.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company} {quarter}".lower()).strip("-")
    return f"{slug}-{date.today()}" + (f"-{suffix}" if suffix else "")


@contextlib.contextmanager
def checkpointer(path: str | Path | None = DEFAULT_PATH):
    """Yield a LangGraph checkpointer, or None when persistence is disabled.

    A context manager because the saver owns a SQLite connection; leaking it
    across a long CLI run is how you get locked databases on Windows.
    """
    if path is None:
        yield None
        return

    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def describe(state_snapshot) -> str:
    """One line describing where a resumed run left off."""
    if state_snapshot is None or not state_snapshot.values:
        return "no saved state"

    values = state_snapshot.values
    done = []
    if values.get("findings"):
        done.append(f"{len(values['findings'])} findings")
    if values.get("outline"):
        done.append(f"{len(values['outline'].sections)} sections planned")
    if values.get("sections"):
        done.append(f"{len(values['sections'])} written")
    if values.get("review"):
        done.append("reviewed")

    next_nodes = ", ".join(state_snapshot.next) if state_snapshot.next else "complete"
    return f"{'; '.join(done) or 'nothing yet'} — next: {next_nodes}"


def load(app, tid: str):
    """Return the saved snapshot for `tid`, or None if there is none."""
    snapshot = app.get_state({"configurable": {"thread_id": tid}})
    return snapshot if snapshot and snapshot.values else None
