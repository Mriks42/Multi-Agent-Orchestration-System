"""Durable runs: a killed orchestrator must resume, not restart.

The distributed layer made tasks recoverable. These tests cover the other half
-- the research, outline and finished sections the orchestrator holds.
"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import FakeChatModel, make_deps

from mas.checkpoint import checkpointer, describe, load, thread_id
from mas.graph import build_graph, run_report
from mas.state import Finding, Outline, Review, Section


class Boom(RuntimeError):
    """Simulates the orchestrator dying part-way through a run."""


def build_models(fail_on: str | None = None, reviews=None):
    """Planner/writer models that can be made to explode at a chosen stage."""
    reviews = iter(reviews or [Review(approved=True)])

    def handler(schema, messages, model):
        name = schema.__name__
        if name == fail_on:
            raise Boom(f"died during {name}")
        if name == "_Queries":
            return schema(queries=["q1"])
        if name == "_Findings":
            return schema(findings=[Finding(claim="c", topic="t", source_ids=[0])])
        if name == "Outline":
            return Outline(
                title="Company X — Q4",
                sections=[Section(heading="Executive Summary", purpose="frame"),
                          Section(heading="Outlook", purpose="predict")],
            )
        raise AssertionError(name)

    writer = FakeChatModel(
        handlers={k: handler for k in ("_Queries", "_Findings", "Outline")},
        text_handler=lambda messages, m: "body",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: next(reviews)})
    return writer, reviewer


# ------------------------------------------------------------------ thread ids


def test_thread_id_is_stable_for_the_same_report_on_the_same_day():
    """Re-running the same command must resume, not start a duplicate."""
    assert thread_id("NVIDIA", "Q4 2025") == thread_id("NVIDIA", "Q4 2025")
    assert thread_id("NVIDIA", "Q4 2025") == f"nvidia-q4-2025-{date.today()}"


def test_thread_id_separates_different_reports():
    assert thread_id("NVIDIA", "Q4 2025") != thread_id("Shopify", "Q4 2025")
    assert thread_id("NVIDIA", "Q4 2025") != thread_id("NVIDIA", "Q3 2025")


def test_suffix_forces_a_fresh_run():
    assert thread_id("NVIDIA", "Q4", "abc123") != thread_id("NVIDIA", "Q4")


# ---------------------------------------------------------------- persistence


def test_disabled_checkpointer_yields_none():
    with checkpointer(None) as saver:
        assert saver is None


def test_state_survives_the_process_that_created_it(tmp_path):
    """The point of persistence: a new saver sees the previous run's work."""
    db = tmp_path / "runs.db"
    tid = "run-1"
    writer, reviewer = build_models()

    with checkpointer(db) as saver:
        run_report(make_deps(writer, reviewer), "Company X", "Q4",
                   checkpointer=saver, thread_id=tid)

    # A completely separate saver, as a restarted process would have.
    with checkpointer(db) as saver2:
        app = build_graph(make_deps(*build_models()), checkpointer=saver2)
        snapshot = load(app, tid)

    assert snapshot is not None
    assert snapshot.values["company"] == "Company X"
    assert len(snapshot.values["sections"]) == 2


def test_resuming_after_a_crash_skips_the_work_already_done(tmp_path):
    """The whole point: research and planning must not run twice."""
    db = tmp_path / "runs.db"
    tid = "crashy-run"

    # First attempt dies in the reviewer, after research/planning/writing.
    writer, reviewer = build_models()
    reviewer.handlers["Review"] = lambda s, m, mo: (_ for _ in ()).throw(Boom("reviewer died"))
    with checkpointer(db) as saver:
        with pytest.raises(Boom):
            run_report(make_deps(writer, reviewer), "Company X", "Q4",
                       checkpointer=saver, thread_id=tid)

    research_calls_first_attempt = sum(1 for name, _ in writer.calls if name == "_Findings")
    assert research_calls_first_attempt == 1

    # Second attempt: same thread, healthy models.
    writer2, reviewer2 = build_models()
    with checkpointer(db) as saver:
        final = run_report(make_deps(writer2, reviewer2), "Company X", "Q4",
                           checkpointer=saver, thread_id=tid)

    assert final["review"].approved is True
    assert final["draft"], "the resumed run must produce the report"

    redone = [name for name, _ in writer2.calls]
    assert "_Findings" not in redone, "research must not re-run on resume"
    assert "Outline" not in redone, "planning must not re-run on resume"


def test_a_fresh_thread_id_reruns_everything(tmp_path):
    """--fresh must not silently pick up the old run."""
    db = tmp_path / "runs.db"
    writer, reviewer = build_models()
    with checkpointer(db) as saver:
        run_report(make_deps(writer, reviewer), "Company X", "Q4",
                   checkpointer=saver, thread_id="run-a")

    writer2, reviewer2 = build_models()
    with checkpointer(db) as saver:
        run_report(make_deps(writer2, reviewer2), "Company X", "Q4",
                   checkpointer=saver, thread_id="run-b")

    assert "_Findings" in [name for name, _ in writer2.calls], "a new thread starts over"


def test_running_without_a_checkpointer_still_works(tmp_path):
    """Persistence is opt-in; the default path must be unaffected."""
    writer, reviewer = build_models()
    final = run_report(make_deps(writer, reviewer), "Company X", "Q4")
    assert final["draft"]


# -------------------------------------------------------------------- describe


def test_describe_reports_where_a_run_left_off(tmp_path):
    db = tmp_path / "runs.db"
    writer, reviewer = build_models()
    with checkpointer(db) as saver:
        run_report(make_deps(writer, reviewer), "Company X", "Q4",
                   checkpointer=saver, thread_id="t1")
        app = build_graph(make_deps(*build_models()), checkpointer=saver)
        text = describe(load(app, "t1"))

    assert "findings" in text and "written" in text and "next:" in text


def test_describe_handles_a_missing_snapshot():
    assert describe(None) == "no saved state"


def test_load_returns_none_for_an_unknown_thread(tmp_path):
    with checkpointer(tmp_path / "runs.db") as saver:
        app = build_graph(make_deps(*build_models()), checkpointer=saver)
        assert load(app, "never-ran") is None
