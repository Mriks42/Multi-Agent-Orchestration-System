"""`mas-ablate`: run one company under several configurations and compare."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .ablate import VARIANTS, discriminates, run_ablation

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas-ablate",
        description="Hold the company fixed and vary the system, to see what a change does.",
    )
    parser.add_argument("--company", default="NVIDIA")
    parser.add_argument("--quarter", default="Q4 2025")
    parser.add_argument(
        "--variants", nargs="+", help=f"Subset of: {', '.join(v.name for v in VARIANTS)}"
    )
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge")
    parser.add_argument("--out", type=Path, default=Path("evals"))
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    chosen = VARIANTS
    if args.variants:
        names = set(args.variants)
        chosen = [v for v in VARIANTS if v.name in names]
        if not chosen:
            console.print(f"[red]No variant matched {sorted(names)}[/red]")
            return 2

    console.print(
        f"[bold]Ablation:[/bold] {args.company} {args.quarter}, "
        f"{len(chosen)} variant(s)\n"
    )
    for v in chosen:
        console.print(f"  [dim]{v.name:16} {v.why}[/dim]")
    console.print()

    def progress(run):
        if run.metrics.error:
            console.print(f"  [red]FAIL[/red] {run.variant.name}: {run.metrics.error}")
        else:
            m = run.metrics
            console.print(
                f"  [green]OK[/green] {run.variant.name:16} "
                f"[dim]{m.citation_count} citations | {m.unattributed_figure_count} unattributed "
                f"| sourced {m.sourced_finding_rate:.0%} | {m.duration_s}s[/dim]"
            )

    runs = run_ablation(
        args.company, args.quarter, variants=chosen,
        judge=not args.no_judge, on_variant=progress,
    )

    table = Table(title=f"{args.company} {args.quarter} under each configuration")
    table.add_column("variant")
    table.add_column("sourced", justify="right")
    table.add_column("cites", justify="right")
    table.add_column("unattributed", justify="right")
    table.add_column("words", justify="right")
    table.add_column("judge overall", justify="right")
    table.add_column("grounding", justify="right")
    for run in runs:
        m = run.metrics
        if m.error:
            table.add_row(run.variant.name, "—", "—", "—", "—", "—", "[red]failed[/red]")
            continue
        table.add_row(
            run.variant.name,
            f"{m.sourced_finding_rate:.0%}",
            str(m.citation_count),
            str(m.unattributed_figure_count),
            str(m.word_count),
            str(run.judge.get("overall", "—")),
            str(run.judge.get("grounding", "—")),
        )
    console.print()
    console.print(table)

    # The question the ablation exists to answer.
    if any(r.variant.name == "no-search" for r in runs):
        check = Table(title="Can each measure tell the fabricated report from the real one?")
        check.add_column("measure")
        check.add_column("baseline", justify="right")
        check.add_column("no-search", justify="right")
        check.add_column("")
        for key in ("sourced_finding_rate", "citation_count", "unattributed_figure_count",
                    "grounding", "overall"):
            d = discriminates(runs, key)
            if d.get("verdict") == "not run":
                continue
            colour = {"correct": "[green]correct[/green]",
                      "blind": "[yellow]BLIND — cannot tell them apart[/yellow]",
                      "inverted": "[red]INVERTED — scores the fabrication better[/red]"}
            check.add_row(
                key, str(d["baseline"]), str(d["no_search"]), colour[d["verdict"]],
            )
        console.print()
        console.print(check)

    label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"ablation-{label}.json"
    path.write_text(json.dumps({
        "company": args.company,
        "quarter": args.quarter,
        "label": label,
        "runs": [
            {
                "variant": r.variant.name,
                "why": r.variant.why,
                "expect": r.variant.expect,
                "metrics": r.metrics.to_dict(),
                "judge": r.judge,
            }
            for r in runs
        ],
    }, indent=2), encoding="utf-8")
    console.print(f"\n[bold]Written to[/bold] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
