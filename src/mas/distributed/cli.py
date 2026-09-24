"""Worker entry point: `mas-worker` starts one process that writes sections.

Run as many as you like, on as many machines as can reach the queue:

    mas-worker --queue mas-queue.db          # terminal 1
    mas-worker --queue mas-queue.db          # terminal 2
    mas --company "NVIDIA" --quarter "Q4 2025" --distributed
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from ..config import load_settings
from ..deps import Deps
from ..llm import MissingAPIKey
from . import open_broker
from .worker import Worker, default_worker_id

log = logging.getLogger("mas.worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas-worker",
        description="Run a worker that writes report sections from the shared queue.",
    )
    parser.add_argument(
        "--queue", default=None,
        help="Shared queue: a file path (SQLite) or a redis:// URL",
    )
    parser.add_argument("--id", default=None, help="Worker id (default: host-pid)")
    parser.add_argument(
        "--max-tasks", type=int, default=None, help="Exit after handling this many tasks"
    )
    parser.add_argument(
        "--lease", type=float, default=None, help="Lease seconds before a task is reclaimed"
    )
    parser.add_argument("--quiet", action="store_true", help="Warnings only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings(lease_seconds=args.lease)
    try:
        deps = Deps.from_settings(settings)
    except MissingAPIKey as exc:
        print(exc, file=sys.stderr)
        return 2

    broker = open_broker(args.queue or settings.broker_path)
    worker = Worker(
        broker,
        deps,
        worker_id=args.id or default_worker_id(),
        lease_seconds=settings.lease_seconds,
    )
    try:
        worker.run_forever(max_tasks=args.max_tasks)
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
