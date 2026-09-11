"""`mas-serve`: run the web UI."""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from .access import MODES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas-serve", description="Serve the report UI and its API."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--max-revisions", type=int, default=2,
        help="Complete drafts per report, not revisions (default 2: draft, "
             "review, revise the flagged sections, review again). 1 disables "
             "the revision loop entirely -- the budget is spent by the time the "
             "Reviewer first speaks, so its objections publish unfixed.",
    )
    parser.add_argument(
        "--access", choices=MODES, default=None,
        help="Who may spend the API key. 'open' (the local default) lets anyone "
             "run a report; 'gallery' serves saved reports to everyone and needs "
             "a code to run a new one; 'locked' needs a code for anything. "
             "Defaults to $MAS_ACCESS, else open. Deployments should set this.",
    )
    parser.add_argument("--reload", action="store_true", help="Reload on code changes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import uvicorn

    from .access import Access, Misconfigured
    from .app import create_app

    try:
        access = Access(mode=args.access) if args.access else None
    except Misconfigured as exc:
        # Refuse to start rather than silently serving an ungated key.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"  http://{args.host}:{args.port}")
    uvicorn.run(
        create_app(max_revisions=args.max_revisions, access=access),
        host=args.host, port=args.port, log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
