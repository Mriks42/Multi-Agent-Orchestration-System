"""Command line entry point: `mas --company "Company X" --quarter Q4`."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from uuid import uuid4

from .checkpoint import DEFAULT_PATH, checkpointer, describe, load, thread_id
from .config import load_settings
from .deps import Deps
from .graph import build_graph, run_report
from .distributed.broker import BrokerError
from .llm import MissingAPIKey
from .state import ReportState

console = Console()

NODE_LABELS = {
    "research": "Research Agent",
    "planning": "Planning Agent",
    # Sections fan out to parallel `write_section` branches; `assemble` is the
    # join that reports the pass as a whole.
    "assemble": "Writer Agent",
    "reviewer": "Reviewer Agent",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "report"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas",
        description="Generate a market research report with a team of coordinated agents.",
    )
    parser.add_argument("--company", required=True, help="Company the report is about")
    parser.add_argument("--quarter", default="Q4", help="Reporting period, e.g. Q4 2025")
    parser.add_argument("--focus", default="", help="Extra angle to emphasise")
    parser.add_argument("--out", type=Path, help="Where to write the report (default: reports/)")
    parser.add_argument("--model", help="Override the drafting model")
    parser.add_argument("--reviewer-model", help="Override the reviewing model")
    parser.add_argument(
        "--max-revisions", type=int, help="Reviewer -> Writer loops allowed before publishing"
    )
    parser.add_argument(
        "--no-search", action="store_true", help="Skip web search; rely on model knowledge only"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Log every agent call")
    parser.add_argument(
        "--distributed",
        action="store_true",
        help="Farm section writing out to mas-worker processes via a shared queue",
    )
    parser.add_argument("--queue", default=None, help="Queue file for --distributed")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Persist progress and resume this report if it was interrupted",
    )
    parser.add_argument("--runs-db", default=None, help="Checkpoint file for --resume")
    parser.add_argument(
        "--fresh", action="store_true", help="With --resume, ignore saved state and start over"
    )
    return parser


def _report_progress(node: str, update: dict) -> None:
    label = NODE_LABELS.get(node, node)
    for line in update.get("trace", []):
        detail = line.split(":", 1)[-1].strip()
        console.print(f"  [green]OK[/green] [bold]{label}[/bold] — {detail}")


def _summarise(state: ReportState) -> None:
    review = state.get("review")
    console.print()
    if review is None:
        return
    if review.approved:
        console.print("[bold green]Reviewer approved the report.[/bold green]")
    else:
        console.print(
            f"[bold yellow]Published with {len(review.issues)} unresolved issue(s)[/bold yellow] "
            "— revision budget was exhausted."
        )
    for issue in review.issues:
        console.print(f"  [yellow]![/yellow] [{issue.severity}] {issue.section or 'report'}: {issue.problem}")
    if review.summary:
        console.print(f"\n[dim]{review.summary}[/dim]")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Windows terminals default to cp1252, which mangles the em-dashes and box
    # characters in agent headings into "?".
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # already wrapped, or not a real tty
            pass

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings(
        model=args.model,
        reviewer_model=args.reviewer_model,
        max_revisions=args.max_revisions,
        search_backend="none" if args.no_search else None,
    )

    broker = None
    if args.distributed:
        from .distributed import SqliteBroker

        broker = SqliteBroker(args.queue or settings.broker_path)

    try:
        deps = Deps.from_settings(settings, broker=broker)
    except MissingAPIKey as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 2

    console.print(
        f"[bold]Market research report[/bold]: {args.company} — {args.quarter}\n"
        f"[dim]draft model {settings.model} | review model {settings.reviewer_model} | "
        f"up to {settings.max_revisions} revision(s)[/dim]\n"
    )

    tid = None
    if args.resume:
        tid = thread_id(args.company, args.quarter, suffix=uuid4().hex[:6] if args.fresh else "")

    try:
        with checkpointer(args.runs_db or DEFAULT_PATH if args.resume else None) as saver:
            if saver is not None and not args.fresh:
                snapshot = load(build_graph(deps, checkpointer=saver), tid)
                if snapshot:
                    console.print(f"[bold]Resuming[/bold] {tid} — {describe(snapshot)}\n")

            state = run_report(
                deps,
                company=args.company,
                quarter=args.quarter,
                focus=args.focus,
                max_revisions=args.max_revisions,
                on_event=_report_progress,
                checkpointer=saver,
                thread_id=tid,
            )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        if args.resume:
            console.print(
                f"[dim]Progress saved as {tid}. Re-run the same command to continue.[/dim]"
            )
        return 130

    _summarise(state)

    sourced = sum(1 for f in state.get("findings", []) if f.source_ids)
    total = len(state.get("findings", []))
    if total and not sourced:
        console.print(
            "\n[bold red]Every figure in this report is model recollection.[/bold red] "
            "No finding is backed by a retrieved source"
            + (" (search was disabled)." if args.no_search else " (search returned nothing).")
            + " Verify before using."
        )
    elif total - sourced:
        console.print(
            f"\n[yellow]{total - sourced} of {total} findings are unsourced[/yellow] "
            "and rest on model recollection."
        )

    out = args.out or Path("reports") / f"{_slug(args.company)}-{_slug(args.quarter)}-{date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(state.get("draft", ""), encoding="utf-8")
    console.print(f"\n[bold]Report written to[/bold] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
