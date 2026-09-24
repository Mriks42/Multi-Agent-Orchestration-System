"""Broker guarantees: atomic claims, leases, retries, idempotent completion.

These are the properties the distributed path depends on, so they are tested
directly rather than inferred from a working run.

**This is a conformance suite, not a SQLite test.** `Broker` was written
anticipating a second backend -- the commit that introduced it says the
contract "fits Redis for deployment" -- but these tests named `SqliteBroker`
directly, so nothing could check that claim. Every test below runs against each
registered backend instead, which is what makes `Broker` an executable contract
rather than a description of the one implementation that exists.

**Adding a backend** is one entry in `BACKENDS`. A builder takes `tmp_path` and
returns a `Backend` whose `connect()` opens a *new client on the same queue* --
the atomicity test needs several, because that is what separate processes do.
For Redis that means a URL plus a per-test key prefix, and a skip mark when no
server is reachable, so the suite stays runnable offline:

    def _redis(tmp_path):
        url = os.getenv("MAS_TEST_REDIS_URL", "redis://localhost:6379/15")
        prefix = f"mastest:{uuid4().hex}:"
        return Backend("redis", lambda: RedisBroker(url, prefix=prefix))

`renew` and `stats` are deliberately *not* on the protocol. `worker.py` reaches
for `renew` with `getattr` and falls back to a generous lease when a backend
lacks it, so the tests covering them skip rather than fail -- an optional
capability must not become a hidden requirement the moment someone writes a
second backend.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable

import pytest

from mas.distributed import BrokerError, RedisBroker, SqliteBroker, TaskState

REDIS_URL = os.getenv("MAS_TEST_REDIS_URL", "redis://localhost:6379/15")


class Backend:
    """One broker implementation, plus the means to reconnect to its queue."""

    def __init__(self, name: str, opener: Callable[[], object]) -> None:
        self.name = name
        self.connect = opener
        """A fresh client on the same queue -- what a separate process opens.

        The caller closes what it opens, and `client()` below is the way to do
        that. A SQLite connection may only be closed on the thread that created
        it, so a fixture cannot tidy up after the threads in the atomicity test.
        """


@contextmanager
def client(backend: Backend):
    """A connection that closes on the thread that opened it."""
    conn = backend.connect()
    try:
        yield conn
    finally:
        conn.close()


def _sqlite(tmp_path) -> Backend:
    path = tmp_path / "queue.db"
    return Backend("sqlite", lambda: SqliteBroker(path, poll_interval=0.01))


def _redis(tmp_path) -> Backend:
    # A prefix per test, so runs never collide and nothing needs flushing --
    # the suite must be safe to point at a Redis that holds other things.
    prefix = f"mastest:{uuid.uuid4().hex}:"
    return Backend("redis", lambda: RedisBroker(REDIS_URL, prefix=prefix, poll_interval=0.01))


def _redis_unreachable() -> str:
    """Skip reason when no server answers, so the suite stays green offline."""
    try:
        import redis
    except ImportError:
        return "redis-py not installed (pip install -e '.[redis]')"
    try:
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping()
    except Exception as exc:
        return f"no Redis at {REDIS_URL}: {type(exc).__name__}"
    return ""


BACKENDS = [
    pytest.param(_sqlite, id="sqlite"),
    pytest.param(
        _redis,
        id="redis",
        marks=pytest.mark.skipif(bool(_redis_unreachable()), reason=_redis_unreachable()),
    ),
]


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path) -> Backend:
    return request.param(tmp_path)


@pytest.fixture
def broker(backend):
    with client(backend) as conn:
        yield conn


def requires(broker, capability: str):
    """Skip when a backend does not offer an optional part of the contract."""
    if not hasattr(broker, capability):
        pytest.skip(f"{type(broker).__name__} does not implement optional {capability}()")


def test_submitted_tasks_start_pending_and_claim_in_order(broker):
    broker.submit("run1", [{"heading": "A"}, {"heading": "B"}])

    first = broker.claim("w1")
    second = broker.claim("w2")
    assert [first.payload["heading"], second.payload["heading"]] == ["A", "B"]
    assert first.state is TaskState.RUNNING
    assert broker.claim("w3") is None, "queue should be empty"


def test_two_workers_never_claim_the_same_task(backend):
    """The core safety property: a claim is atomic across processes."""
    with client(backend) as submitter:
        submitter.submit("run1", [{"heading": f"S{i}"} for i in range(20)])

    claimed: list[str] = []
    lock = threading.Lock()

    def drain(worker_id):
        # Each thread opens and closes its own connection, as separate
        # processes would -- and as SQLite's thread affinity requires.
        with client(backend) as own:
            while (task := own.claim(worker_id)) is not None:
                with lock:
                    claimed.append(task.task_id)

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
    requires(broker, "renew")
    broker.submit("run1", [{"heading": "A"}])
    task = broker.claim("w1", lease_seconds=0.05)

    broker.renew(task.task_id, lease_seconds=5.0)
    time.sleep(0.1)

    assert broker.reclaim_expired() == 0
    assert broker.claim("thief") is None


def test_stats_report_progress_by_state(broker):
    requires(broker, "stats")
    broker.submit("run1", [{"heading": "A"}, {"heading": "B"}])
    claimed = broker.claim("w1")
    broker.complete(claimed.task_id, {"sections": {}})

    assert broker.stats("run1") == {"done": 1, "pending": 1}


def test_every_protocol_method_is_implemented(broker):
    """A backend that misses part of the contract must fail here, not in a run.

    `Broker` is a `Protocol`, and a structural one is not checked at import: a
    backend missing `reclaim_expired` imports cleanly and strands a run months
    later. This is the check that makes registering a backend above mean
    something.
    """
    required = ["submit", "claim", "complete", "fail", "reclaim_expired", "gather", "close"]
    missing = [name for name in required if not callable(getattr(broker, name, None))]
    assert not missing, f"{type(broker).__name__} is missing {missing}"


def test_open_broker_picks_the_backend_from_the_queue_string(tmp_path):
    """`mas` and `mas-worker` must read one --queue the same way.

    A mismatch does not raise: the worker would poll an empty SQLite file while
    the orchestrator waited on Redis, and the run would end in a timeout that
    looks like slow workers rather than a misconfiguration.

    Constructing a RedisBroker connects to nothing -- redis-py dials on first
    use -- so this stays offline even for an address that does not exist.
    """
    from mas.distributed import RedisBroker, SqliteBroker, open_broker

    sqlite = open_broker(tmp_path / "queue.db")
    assert isinstance(sqlite, SqliteBroker)
    sqlite.close()

    for url in ("redis://localhost:6379/15", "rediss://nonexistent.invalid:6380/0"):
        broker = open_broker(url)
        assert isinstance(broker, RedisBroker), url
        broker.close()
