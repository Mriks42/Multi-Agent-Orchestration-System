"""A broker backed by Redis.

The second implementation of `Broker`, and the point of having a protocol: the
orchestrator, the workers and every guarantee test are unchanged: only the
constructor differs. `SqliteBroker` stays the default because it needs no
server; this one is for deployment, where several machines share a queue that
no single one of them owns.

Where SQLite takes a write lock, Redis runs a Lua script. `EVAL` is atomic --
the server executes the whole script before any other command -- so the
read-then-write in `claim` cannot interleave with another worker's, which is
the same property `BEGIN IMMEDIATE` buys in the SQLite backend. Everything else
follows from that one choice.

Keys, all under `prefix`:

    {p}task:{task_id}   HASH   the task's fields
    {p}pending          ZSET   claimable task ids, scored by created_at
    {p}running          ZSET   leased task ids, scored by lease_until
    {p}run:{run_id}     SET    task ids in a run, for gather() and stats()

`pending` is a ZSET rather than a list because the contract is "oldest first"
and a reclaimed task must return to its place in that order, not to the back of
a queue. `running` is scored by lease expiry so `reclaim_expired` is one range
query instead of a scan of every task.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .broker import Broker, BrokerError, Task, TaskState

log = logging.getLogger(__name__)

# Take the oldest claimable task and lease it, in one indivisible step.
# KEYS: pending, running   ARGV: worker_id, lease_until, task-key prefix
_CLAIM = """
local ids = redis.call('ZRANGE', KEYS[1], 0, 0)
if #ids == 0 then return nil end
local id = ids[1]
local key = ARGV[3] .. id
redis.call('ZREM', KEYS[1], id)
redis.call('HSET', key, 'state', 'running', 'worker_id', ARGV[1], 'lease_until', ARGV[2])
redis.call('HINCRBY', key, 'attempts', 1)
redis.call('ZADD', KEYS[2], ARGV[2], id)
return cjson.encode(redis.call('HGETALL', key))
"""

# First result wins: a late answer from a worker whose lease expired must not
# overwrite the one another worker already produced.
# KEYS: running   ARGV: task_id, result_json, task-key prefix
_COMPLETE = """
local key = ARGV[3] .. ARGV[1]
if redis.call('EXISTS', key) == 0 then return 0 end
if redis.call('HGET', key, 'state') == 'done' then return 0 end
redis.call('HSET', key, 'state', 'done', 'result', ARGV[2], 'error', '')
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""

# Requeue while attempts remain, otherwise mark dead so gather() fails fast.
# KEYS: pending, running   ARGV: task_id, error, task-key prefix
_FAIL = """
local key = ARGV[3] .. ARGV[1]
if redis.call('EXISTS', key) == 0 then return 0 end
local attempts = tonumber(redis.call('HGET', key, 'attempts'))
local max = tonumber(redis.call('HGET', key, 'max_attempts'))
redis.call('ZREM', KEYS[2], ARGV[1])
if attempts >= max then
  redis.call('HSET', key, 'state', 'failed', 'error', ARGV[2], 'worker_id', '', 'lease_until', 0)
  return 1
end
redis.call('HSET', key, 'state', 'pending', 'error', ARGV[2], 'worker_id', '', 'lease_until', 0)
redis.call('ZADD', KEYS[1], redis.call('HGET', key, 'created_at'), ARGV[1])
return 0
"""

# A lapsed lease is a delay, not a lost report -- unless the attempts are gone,
# in which case it is a death and gather() should not wait out its timeout.
# KEYS: pending, running   ARGV: now, task-key prefix
_RECLAIM = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
local reclaimed = 0
for _, id in ipairs(expired) do
  local key = ARGV[2] .. id
  local attempts = tonumber(redis.call('HGET', key, 'attempts'))
  local max = tonumber(redis.call('HGET', key, 'max_attempts'))
  redis.call('ZREM', KEYS[2], id)
  if attempts < max then
    redis.call('HSET', key, 'state', 'pending', 'worker_id', '', 'lease_until', 0)
    redis.call('ZADD', KEYS[1], redis.call('HGET', key, 'created_at'), id)
    reclaimed = reclaimed + 1
  else
    redis.call('HSET', key, 'state', 'failed', 'worker_id', '', 'lease_until', 0,
               'error', 'lease expired, attempts exhausted')
  end
end
return reclaimed
"""

# Only a task still running may extend its lease.
# KEYS: running   ARGV: task_id, lease_until, task-key prefix
_RENEW = """
local key = ARGV[3] .. ARGV[1]
if redis.call('HGET', key, 'state') ~= 'running' then return 0 end
redis.call('HSET', key, 'lease_until', ARGV[2])
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
return 1
"""


class RedisBroker(Broker):
    """Multi-machine task queue backed by Redis."""

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        prefix: str = "mas:",
        poll_interval: float = 0.2,
        **kwargs: Any,
    ):
        import redis  # imported here: the dependency is optional, the import is not free

        self.url = url
        self.prefix = prefix
        self.poll_interval = poll_interval
        self._redis = redis.Redis.from_url(url, decode_responses=True, **kwargs)
        self._claim = self._redis.register_script(_CLAIM)
        self._complete = self._redis.register_script(_COMPLETE)
        self._fail = self._redis.register_script(_FAIL)
        self._reclaim = self._redis.register_script(_RECLAIM)
        self._renew = self._redis.register_script(_RENEW)

    # ------------------------------------------------------------------ writes

    def submit(self, run_id: str, payloads: list[dict]) -> list[str]:
        tasks = [Task(run_id=run_id, payload=p) for p in payloads]
        now = time.time()
        pipe = self._redis.pipeline(transaction=True)
        for i, task in enumerate(tasks):
            # Distinct, increasing scores: submit() is called once with the
            # whole wave, and time.time() can return the same value twice.
            created = now + i * 1e-6
            pipe.hset(
                self._key(task.task_id),
                mapping={
                    "task_id": task.task_id,
                    "run_id": run_id,
                    "payload": json.dumps(task.payload),
                    "state": TaskState.PENDING.value,
                    "attempts": 0,
                    "max_attempts": task.max_attempts,
                    "lease_until": 0,
                    "worker_id": "",
                    "result": "",
                    "error": "",
                    "created_at": created,
                },
            )
            pipe.zadd(self._k("pending"), {task.task_id: created})
            pipe.sadd(self._k(f"run:{run_id}"), task.task_id)
        pipe.execute()
        log.info("submitted %d task(s) for run %s", len(tasks), run_id)
        return [t.task_id for t in tasks]

    def claim(self, worker_id: str, lease_seconds: float = 60.0) -> Task | None:
        raw = self._claim(
            keys=[self._k("pending"), self._k("running")],
            args=[worker_id, time.time() + lease_seconds, self._key("")],
        )
        if raw is None:
            return None
        return self._to_task(_flat_to_dict(json.loads(raw)))

    def complete(self, task_id: str, result: Any) -> None:
        self._complete(
            keys=[self._k("running")],
            args=[task_id, json.dumps(result), self._key("")],
        )

    def fail(self, task_id: str, error: str) -> None:
        dead = self._fail(
            keys=[self._k("pending"), self._k("running")],
            args=[task_id, error, self._key("")],
        )
        log.warning("task %s failed (%s): %s", task_id, "dead" if dead else "requeued", error)

    def renew(self, task_id: str, lease_seconds: float) -> None:
        """Extend a running task's lease. Called while an LLM call is in flight."""
        self._renew(
            keys=[self._k("running")],
            args=[task_id, time.time() + lease_seconds, self._key("")],
        )

    def reclaim_expired(self) -> int:
        """Return tasks from workers that stopped renewing their lease."""
        reclaimed = int(
            self._reclaim(
                keys=[self._k("pending"), self._k("running")],
                args=[time.time(), self._key("")],
            )
        )
        if reclaimed:
            log.warning("reclaimed %d task(s) from expired leases", reclaimed)
        return reclaimed

    # ------------------------------------------------------------------- reads

    def gather(self, run_id: str, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            self.reclaim_expired()
            tasks = self._run_tasks(run_id)
            if not tasks:
                return {}

            dead = [t for t in tasks if t["state"] == TaskState.FAILED.value]
            if dead:
                raise BrokerError(
                    f"{len(dead)} task(s) in run {run_id} failed permanently: {dead[0]['error']}"
                )
            if all(t["state"] == TaskState.DONE.value for t in tasks):
                return {t["task_id"]: json.loads(t["result"]) for t in tasks}

            if time.time() > deadline:
                pending = sum(1 for t in tasks if t["state"] != TaskState.DONE.value)
                raise BrokerError(
                    f"timed out after {timeout}s with {pending}/{len(tasks)} task(s) unfinished"
                )
            time.sleep(self.poll_interval)

    def stats(self, run_id: str) -> dict[str, int]:
        """Task counts by state, for progress display."""
        counts: dict[str, int] = {}
        for task in self._run_tasks(run_id):
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        return counts

    def close(self) -> None:
        self._redis.close()

    # --------------------------------------------------------------- internals

    def _k(self, suffix: str) -> str:
        return f"{self.prefix}{suffix}"

    def _key(self, task_id: str) -> str:
        return f"{self.prefix}task:{task_id}"

    def _run_tasks(self, run_id: str) -> list[dict]:
        ids = self._redis.smembers(self._k(f"run:{run_id}"))
        if not ids:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for task_id in ids:
            pipe.hgetall(self._key(task_id))
        return [h for h in pipe.execute() if h]

    @staticmethod
    def _to_task(h: dict) -> Task:
        return Task(
            task_id=h["task_id"],
            run_id=h["run_id"],
            payload=json.loads(h["payload"]),
            state=TaskState(h["state"]),
            attempts=int(h["attempts"]),
            max_attempts=int(h["max_attempts"]),
            lease_until=float(h["lease_until"]),
            worker_id=h["worker_id"],
            result=json.loads(h["result"]) if h["result"] else None,
            error=h["error"],
        )


def _flat_to_dict(flat: list) -> dict:
    """HGETALL through cjson arrives as [k1, v1, k2, v2, ...]."""
    return dict(zip(flat[::2], flat[1::2]))
