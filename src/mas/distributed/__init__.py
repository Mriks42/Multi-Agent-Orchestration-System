"""Distributed execution: section writing farmed out to worker processes."""

from .broker import Broker, BrokerError, Task, TaskState
# Importing this is safe without redis-py: the client import lives inside
# RedisBroker.__init__, so only *constructing* one needs the optional extra.
from .redis_broker import RedisBroker
from .sqlite_broker import SqliteBroker
from .worker import Worker, default_worker_id, run_task

def open_broker(queue, **kwargs) -> Broker:
    """Pick a backend from the queue string: a URL means Redis, a path SQLite.

    One place decides, so `mas` and `mas-worker` cannot read the same --queue
    differently. A worker on SQLite while the orchestrator is on Redis would
    not error -- it would sit on an empty queue while the run timed out.
    """
    text = str(queue)
    if text.startswith(("redis://", "rediss://", "unix://")):
        return RedisBroker(text, **kwargs)
    return SqliteBroker(text, **kwargs)


__all__ = [
    "Broker",
    "BrokerError",
    "Task",
    "TaskState",
    "SqliteBroker",
    "RedisBroker",
    "open_broker",
    "Worker",
    "run_task",
    "default_worker_id",
]
