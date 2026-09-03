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

from .config import load_settings
from .deps import Deps
from .graph import run_report
from .llm import MissingAPIKey
from .state import ReportState

console = Console()

NODE_LABELS = {
    "research": "Research Agent",
    "planning": "Planning Agent",
    "writer": "Writer Agent",
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

    try:
        deps = Deps.from_settings(settings)
    except MissingAPIKey as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 2

    console.print(
        f"[bold]Market research report[/bold]: {args.company} — {args.quarter}\n"
        f"[dim]draft model {settings.model} | review model {settings.reviewer_model} | "
        f"up to {settings.max_revisions} revision(s)[/dim]\n"
    )

    try:
        state = run_report(
            deps,
            company=args.company,
            quarter=args.quarter,
            focus=args.focus,
            max_revisions=args.max_revisions,
            on_event=_report_progress,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130

    _summarise(state)

    out = args.out or Path("reports") / f"{_slug(args.company)}-{_slug(args.quarter)}-{date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(state.get("draft", ""), encoding="utf-8")
    console.print(f"\n[bold]Report written to[/bold] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
