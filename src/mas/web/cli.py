"""`mas-serve`: run the web UI."""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv


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
    parser.add_argument("--reload", action="store_true", help="Reload on code changes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import uvicorn

    from .app import create_app

    print(f"  http://{args.host}:{args.port}")
    uvicorn.run(
        create_app(max_revisions=args.max_revisions),
        host=args.host, port=args.port, log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
