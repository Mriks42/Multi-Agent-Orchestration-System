"""Distributed execution: workers, crash recovery, and graph equivalence.

The subprocess test is the one that earns the word "distributed" -- separate
OS processes, coordinating only through the queue file.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from conftest import FakeChatModel, make_deps

from mas.distributed import SqliteBroker, Worker
from mas.distributed.worker import run_task
from mas.graph import run_report
from mas.state import Finding, Issue, Outline, Review, Section

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def broker(tmp_path):
    b = SqliteBroker(tmp_path / "queue.db", poll_interval=0.01)
    yield b
    b.close()


def _writer_model(text="written by worker"):
    return FakeChatModel(text_handler=lambda messages, m: text)


def _payload(heading="Outlook", issues=None):
    return {
        "heading": heading,
        "purpose": "predict",
        "key_points": ["a point"],
        "target_words": 100,
        "title": "Report",
        "audience": "executives",
        "company": "Company X",
        "quarter": "Q4",
        "findings": "- (high) [t] a claim sources=[0]",
        "sources": "[0] a source",
        "current": "old text",
        "issues": issues or [],
        "siblings": {},
    }


# --------------------------------------------------------------------- worker


def test_worker_claims_a_task_and_publishes_the_result(broker):
    broker.submit("run1", [_payload()])
    worker = Worker(broker, make_deps(_writer_model()), worker_id="w1")

    assert worker.run_once() is True
    assert worker.completed == 1
    results = broker.gather("run1", timeout=2)
    assert list(results.values())[0]["sections"] == {"Outlook": "written by worker"}


def test_worker_reports_no_work_on_an_empty_queue(broker):
    worker = Worker(broker, make_deps(_writer_model()), worker_id="w1")
    assert worker.run_once() is False


def test_worker_failure_requeues_the_task_for_someone_else(broker):
    """A transient API error must cost a retry, not the report."""
    broken = FakeChatModel(text_handler=lambda m, model: (_ for _ in ()).throw(RuntimeError("api down")))
    broker.submit("run1", [_payload()])

    failing = Worker(broker, make_deps(broken), worker_id="broken")
    assert failing.run_once() is True
    assert failing.failed == 1

    healthy = Worker(broker, make_deps(_writer_model("recovered")), worker_id="healthy")
    assert healthy.run_once() is True
    assert list(broker.gather("run1", timeout=2).values())[0]["sections"]["Outlook"] == "recovered"


def test_issues_survive_the_json_round_trip_to_a_worker(broker):
    """Issues cross the wire as dicts; the writer needs Issue objects back."""
    seen = []
    model = FakeChatModel(text_handler=lambda messages, m: seen.append(messages) or "revised")
    payload = _payload(issues=[Issue(severity="major", problem="thin", fix="add detail").model_dump()])

    broker.submit("run1", [payload])
    Worker(broker, make_deps(model), worker_id="w1").run_once()

    assert "add detail" in seen[0][1]["content"], "the revise prompt must reach the model"


def test_run_task_is_a_pure_function_of_its_payload():
    """No graph, no state -- which is what makes the task portable."""
    result = run_task(make_deps(_writer_model("body")), type("T", (), {"payload": _payload()})())
    assert result == {"sections": {"Outlook": "body"}}


# ------------------------------------------------------------- crash recovery


def test_a_dead_worker_does_not_lose_the_task(broker):
    """Claim a task, never finish it, and let another worker recover it."""
    broker.submit("run1", [_payload()])

    abandoned = broker.claim("crashed-worker", lease_seconds=0.05)
    assert abandoned is not None
    time.sleep(0.1)  # the worker "dies" -- no completion, no renewal

    survivor = Worker(broker, make_deps(_writer_model("recovered")), worker_id="survivor")
    assert survivor.run_once() is True, "reclaim_expired should hand over the abandoned task"

    results = broker.gather("run1", timeout=2)
    assert list(results.values())[0]["sections"]["Outlook"] == "recovered"


# ------------------------------------------------------- real OS processes


@pytest.mark.slow
def test_separate_processes_share_the_queue(tmp_path):
    """Two real subprocesses drain one queue, coordinating only through the file."""
    queue = tmp_path / "queue.db"
    broker = SqliteBroker(queue, poll_interval=0.01)
    broker.submit("run1", [_payload(f"Section {i}") for i in range(6)])

    script = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {SRC!r}); sys.path.insert(0, {str(Path(__file__).parent)!r})
        from conftest import FakeChatModel, make_deps
        from mas.distributed import SqliteBroker, Worker
        import os
        broker = SqliteBroker({str(queue)!r}, poll_interval=0.01)
        model = FakeChatModel(text_handler=lambda m, mo: "by pid %d" % os.getpid())
        w = Worker(broker, make_deps(model), worker_id="pid-%d" % os.getpid(), idle_sleep=0.02)
        w.run_forever(max_tasks=3)
        broker.close()
    """)

    procs = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    for p in procs:
        p.wait(timeout=60)

    results = broker.gather("run1", timeout=10)
    assert len(results) == 6, "every section must be written exactly once"

    authors = {r["sections"][h] for r in results.values() for h in r["sections"]}
    assert len(authors) == 2, f"work should span both processes, saw {authors}"
    broker.close()


# --------------------------------------------------------- graph equivalence


def _models(reviews):
    it = iter(reviews)

    def handler(schema, messages, model):
        name = schema.__name__
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
        handlers={"_Queries": handler, "_Findings": handler, "Outline": handler},
        text_handler=lambda messages, m: "body",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: next(it)})
    return writer, reviewer


def test_distributed_graph_produces_the_same_report_as_the_local_one(broker, tmp_path):
    """The broker changes where work runs, never what the report says."""
    writer, reviewer = _models([Review(approved=True)])
    local = run_report(make_deps(writer, reviewer), "Company X", "Q4")

    writer2, reviewer2 = _models([Review(approved=True)])
    deps = make_deps(writer2, reviewer2)
    deps.broker = broker

    # A worker drains the queue alongside the graph, as a real deployment would.
    import threading
    stop = threading.Event()

    def drain():
        own = SqliteBroker(broker.path, poll_interval=0.01)
        w = Worker(own, make_deps(FakeChatModel(text_handler=lambda m, mo: "body")), idle_sleep=0.02)
        while not stop.is_set():
            w.run_once()
        own.close()

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        remote = run_report(deps, "Company X", "Q4")
    finally:
        stop.set()
        t.join(timeout=5)

    assert remote["sections"] == local["sections"]
    assert remote["draft"] == local["draft"]
