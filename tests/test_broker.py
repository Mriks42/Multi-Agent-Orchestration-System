"""Broker guarantees: atomic claims, leases, retries, idempotent completion.

These are the properties the distributed path depends on, so they are tested
directly rather than inferred from a working run.
"""

from __future__ import annotations

import threading
import time

import pytest

from mas.distributed import BrokerError, SqliteBroker, TaskState


@pytest.fixture
def broker(tmp_path):
    b = SqliteBroker(tmp_path / "queue.db", poll_interval=0.01)
    yield b
    b.close()


def test_submitted_tasks_start_pending_and_claim_in_order(broker):
    broker.submit("run1", [{"heading": "A"}, {"heading": "B"}])

    first = broker.claim("w1")
    second = broker.claim("w2")
    assert [first.payload["heading"], second.payload["heading"]] == ["A", "B"]
    assert first.state is TaskState.RUNNING
    assert broker.claim("w3") is None, "queue should be empty"


def test_two_workers_never_claim_the_same_task(broker):
    """The core safety property: a claim is atomic across processes."""
    broker.submit("run1", [{"heading": f"S{i}"} for i in range(20)])

    claimed: list[str] = []
    lock = threading.Lock()

    def drain(worker_id):
        # Each thread opens its own connection, as separate processes would.
        own = SqliteBroker(broker.path)
        try:
            while (task := own.claim(worker_id)) is not None:
                with lock:
                    claimed.append(task.task_id)
        finally:
            own.close()

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 20
    assert len(set(claimed)) == 20, "a task was claimed twice"


def test_expired_lease_returns_the_task_to_the_queue(broker):
    """A worker that dies holding a task must not strand the run."""
    broker.submit("run1", [{"heading": "A"}])
    task = broker.claim("doomed-worker", lease_seconds=0.05)
    assert broker.claim("other") is None, "still leased"

    time.sleep(0.1)
    assert broker.reclaim_expired() == 1

    recovered = broker.claim("survivor")
    assert recovered is not None
    assert recovered.task_id == task.task_id
    assert recovered.attempts == 2, "the reclaim counts as a fresh attempt"


def test_failure_requeues_until_attempts_run_out(broker):
    broker.submit("run1", [{"heading": "A"}])

    for attempt in (1, 2):
        task = broker.claim(f"w{attempt}")
        assert task.attempts == attempt, "a requeued task stays claimable"
        broker.fail(task.task_id, "boom")

    # Third attempt exhausts max_attempts=3.
    task = broker.claim("w3")
    assert task.attempts == 3
    broker.fail(task.task_id, "boom")
    assert broker.claim("w4") is None, "a dead task must not be handed out again"


def test_completion_is_idempotent_and_first_result_wins(broker):
    """A late result from a reclaimed worker must not overwrite the real one."""
    broker.submit("run1", [{"heading": "A"}])
    task = broker.claim("w1")

    broker.complete(task.task_id, {"sections": {"A": "real"}})
    broker.complete(task.task_id, {"sections": {"A": "stale"}})

    assert broker.gather("run1")[task.task_id]["sections"]["A"] == "real"


def test_gather_returns_every_result_once_all_finish(broker):
    ids = broker.submit("run1", [{"heading": "A"}, {"heading": "B"}])
    for task_id in ids:
        claimed = broker.claim("w1")
        broker.complete(claimed.task_id, {"sections": {claimed.payload["heading"]: "text"}})

    results = broker.gather("run1")
    assert len(results) == 2
    merged = {h: t for r in results.values() for h, t in r["sections"].items()}
    assert merged == {"A": "text", "B": "text"}


def test_gather_fails_fast_when_a_task_is_permanently_dead(broker):
    broker.submit("run1", [{"heading": "A"}])
    for _ in range(3):
        task = broker.claim("w1")
        broker.fail(task.task_id, "always broken")

    with pytest.raises(BrokerError, match="failed permanently"):
        broker.gather("run1", timeout=1.0)


def test_gather_times_out_rather_than_blocking_forever(broker):
    broker.submit("run1", [{"heading": "A"}])
    with pytest.raises(BrokerError, match="timed out"):
        broker.gather("run1", timeout=0.2)


def test_gather_on_an_unknown_run_returns_empty(broker):
    assert broker.gather("nobody-submitted-this") == {}


def test_renewing_a_lease_prevents_reclamation(broker):
    """A slow LLM call must not look like a dead worker."""
    broker.submit("run1", [{"heading": "A"}])
    task = broker.claim("w1", lease_seconds=0.05)

    broker.renew(task.task_id, lease_seconds=5.0)
    time.sleep(0.1)

    assert broker.reclaim_expired() == 0
    assert broker.claim("thief") is None


def test_stats_report_progress_by_state(broker):
    broker.submit("run1", [{"heading": "A"}, {"heading": "B"}])
    claimed = broker.claim("w1")
    broker.complete(claimed.task_id, {"sections": {}})

    assert broker.stats("run1") == {"done": 1, "pending": 1}
