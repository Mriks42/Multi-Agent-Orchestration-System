"""Eval entry point: `mas-eval` runs the suite and reports against a baseline."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from ..config import load_settings
from ..deps import Deps
from ..llm import MissingAPIKey
from .cases import CASES, SMOKE, by_name
from .runner import comparable, compare, load_baseline, run_suite, save
from .seeded import run_probes, score_probes

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas-eval",
        description="Score the pipeline on a fixed case set and compare to the last run.",
    )
    parser.add_argument("--smoke", action="store_true", help="Two cases instead of the full suite")
    parser.add_argument("--cases", nargs="+", help="Run only these companies")
    parser.add_argument(
        "--probes-only",
        action="store_true",
        help="Run only the seeded-error probes (5 LLM calls, no reports)",
    )
    parser.add_argument("--no-probes", action="store_true", help="Skip the seeded-error probes")
    parser.add_argument("--max-revisions", type=int, default=1, help="Cap revisions (default 1)")
    parser.add_argument("--no-search", action="store_true", help="Disable web search")
    parser.add_argument("--out", type=Path, default=Path("evals"), help="Where results are stored")
    parser.add_argument("--model", help="Override the drafting model")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Also score each report with the pinned LLM judge (1 extra call per case)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def _probe_table(results) -> Table:
    table = Table(title="Seeded-error probes", show_lines=False)
    table.add_column("probe")
    table.add_column("expected")
    table.add_column("flagged")
    table.add_column("")
    for r in results:
        table.add_row(
            r.name,
            "flag" if r.should_flag else "pass",
            "flag" if r.flagged else "pass",
            "[green]ok[/green]" if r.correct else "[red]MISS[/red]",
        )
    return table


def _summary_table(summary: dict) -> Table:
    table = Table(title="Suite summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key in (
        "succeeded", "failed", "sourced_finding_rate", "cited_section_rate",
        "citation_count", "unattributed_figure_count", "orphan_citation_count",
        "approval_rate", "word_count", "duration_s",
    ):
        table.add_row(key, str(summary.get(key, "-")))
    return table


def _diff_table(rows: list[dict]) -> Table:
    table = Table(title="Change vs last run")
    table.add_column("metric")
    table.add_column("was", justify="right")
    table.add_column("now", justify="right")
    table.add_column("delta", justify="right")
    for row in rows:
        colour = "" if row["improved"] is None else ("green" if row["improved"] else "red")
        delta = f"[{colour}]{row['delta']:+}[/{colour}]" if colour else str(row["delta"])
        table.add_row(row["metric"], str(row["baseline"]), str(row["current"]), delta)
    return table


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings(
        model=args.model, search_backend="none" if args.no_search else None
    )
    try:
        deps = Deps.from_settings(settings)
    except MissingAPIKey as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 2

    if args.probes_only:
        results = run_probes(deps)
        console.print(_probe_table(results))
        summary = score_probes(results)
        console.print(
            f"\ncatch rate [bold]{summary['catch_rate']:.0%}[/bold] "
            f"({summary['caught']}/{summary['planted']}), "
            f"false positives [bold]{summary['false_positives']}[/bold]"
            f"/{summary['clean_controls']}"
        )
        return 0 if summary["catch_rate"] == 1.0 and not summary["false_positives"] else 1

    cases = by_name(args.cases) if args.cases else (SMOKE if args.smoke else CASES)
    console.print(f"[bold]Running {len(cases)} case(s)[/bold] on {settings.model}\n")

    def progress(case, metrics):
        if metrics.error:
            console.print(f"  [red]FAIL[/red] {case.label}: {metrics.error}")
        else:
            console.print(
                f"  [green]OK[/green] {case.label} "
                f"[dim]sourced {metrics.sourced_finding_rate:.0%} | "
                f"{metrics.citation_count} citations | "
                f"{metrics.unattributed_figure_count} unattributed | "
                f"{metrics.duration_s}s[/dim]"
            )

    baseline = load_baseline(args.out, like=cases)
    result = run_suite(
        deps, cases, max_revisions=args.max_revisions,
        with_probes=not args.no_probes, judge=args.judge, on_case=progress,
    )

    console.print()
    console.print(_summary_table(result["summary"]))
    console.print(
        f"[dim]over {len(result['cases'])} case(s): "
        + ", ".join(c["company"] for c in result["cases"]) + "[/dim]"
    )

    if result["by_coverage"]:
        table = Table(title="By expected evidence coverage")
        table.add_column("coverage")
        table.add_column("cases", justify="right")
        table.add_column("sourced", justify="right")
        table.add_column("unattributed figures", justify="right")
        for coverage, row in result["by_coverage"].items():
            table.add_row(
                coverage, str(row["cases"]),
                f"{row['sourced_finding_rate']:.0%}",
                str(row["unattributed_figure_count"]),
            )
        console.print(table)

    if "probe_summary" in result:
        ps = result["probe_summary"]
        console.print(
            f"\nReviewer catch rate [bold]{ps['catch_rate']:.0%}[/bold] "
            f"({ps['caught']}/{ps['planted']}), "
            f"false positives {ps['false_positives']}/{ps['clean_controls']}"
        )
        if ps["missed"]:
            console.print(f"  [yellow]missed:[/yellow] {', '.join(ps['missed'])}")

    if "judge_summary" in result:
        js = result["judge_summary"]
        table = Table(title=f"LLM judge ({settings.judge_model}, temp 0)")
        table.add_column("criterion")
        table.add_column("mean 1-5", justify="right")
        for key, value in js.items():
            if key not in ("scored", "failed"):
                table.add_row(key, str(value))
        console.print()
        console.print(table)
        console.print(
            f"[dim]{js['scored']} report(s) scored"
            + (f", {js['failed']} judge failure(s)" if js.get("failed") else "")
            + " — absolute scores are directional; trust pairwise deltas.[/dim]"
        )

    mismatch = comparable(result, baseline)
    if mismatch:
        console.print(f"
[yellow]Not compared to the last run:[/yellow] {mismatch}.")
        baseline = None

    rows = compare(result, baseline)
    if rows:
        console.print()
        console.print(_diff_table(rows))
    elif baseline is None:
        console.print("\n[dim]No baseline yet — this run becomes the baseline.[/dim]")

    path = save(result, args.out)
    console.print(f"\n[bold]Results written to[/bold] {path}")
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
