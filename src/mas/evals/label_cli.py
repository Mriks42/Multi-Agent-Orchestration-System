"""`mas-label`: label report pairs by hand, then score the judge against you.

    mas-label build      # make pairs from stored eval runs
    mas-label            # label them, blind
    mas-label score      # replay the judge and report agreement
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from ..config import load_settings
from ..deps import Deps
from ..llm import MissingAPIKey
from .label import DEFAULT_PATH, LabelSet, pairs_from_runs, score_against_judge
from .metrics import _body

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas-label",
        description="Label report pairs by hand so the LLM judge can be validated.",
    )
    parser.add_argument(
        "action", nargs="?", default="label", choices=["build", "label", "score", "status"]
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--evals", type=Path, default=Path("evals"))
    parser.add_argument("--limit", type=int, default=None, help="Label at most this many")
    return parser


def _build(args) -> int:
    runs = [json.loads(Path(f).read_text(encoding="utf-8"))
            for f in sorted(glob.glob(str(args.evals / "eval-*.json")))]
    if len(runs) < 2:
        console.print(
            f"[yellow]Need at least two stored eval runs to build pairs; found {len(runs)}.[/yellow]\n"
            "[dim]Run `mas-eval --smoke` again after a change, then retry.[/dim]"
        )
        return 1

    existing = LabelSet.load(args.labels)
    keep = {p.item: p for p in existing.pairs if p.labelled}

    fresh = pairs_from_runs(runs)
    merged = [keep.get(p.item, p) for p in fresh]
    labelset = LabelSet(pairs=merged)
    path = labelset.save(args.labels)

    console.print(
        f"{len(merged)} pair(s) written to {path} "
        f"({len(labelset.labelled)} already labelled)."
    )
    return 0


def _label(args) -> int:
    labelset = LabelSet.load(args.labels)
    todo = [p for p in labelset.pairs if not p.labelled]
    if not labelset.pairs:
        console.print("[yellow]No pairs yet. Run `mas-label build` first.[/yellow]")
        return 1
    if not todo:
        console.print(f"All {len(labelset.pairs)} pair(s) labelled. Run `mas-label score`.")
        return 0

    if args.limit:
        todo = todo[: args.limit]

    console.print(
        f"[bold]{len(todo)} pair(s) to label.[/bold] Which report is better overall?\n"
        "[dim]a / b / t (tie) / s (skip) / q (save and quit). "
        "You are not told which is which — that is the point.[/dim]\n"
    )

    for i, pair in enumerate(todo, 1):
        console.rule(f"[bold]{i}/{len(todo)}[/bold]  {pair.company} {pair.quarter}")
        for name, draft in (("A", pair.a), ("B", pair.b)):
            console.print(Panel(_body(draft)[:2200], title=f"REPORT {name}", expand=False))

        while True:
            choice = console.input("[bold]better? (a/b/t/s/q)[/bold] ").strip().lower()
            if choice in ("a", "b", "t", "s", "q"):
                break
            console.print("[dim]a, b, t, s or q[/dim]")

        if choice == "q":
            break
        if choice == "s":
            continue
        pair.human = {"a": "A", "b": "B", "t": "tie"}[choice]

    path = labelset.save(args.labels)
    console.print(f"\n{len(labelset.labelled)} label(s) saved to {path}")
    return 0


def _score(args) -> int:
    labelset = LabelSet.load(args.labels)
    if not labelset.labelled:
        console.print("[yellow]No labels yet. Run `mas-label` first.[/yellow]")
        return 1

    try:
        deps = Deps.from_settings(load_settings())
    except MissingAPIKey as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 2

    console.print(f"Replaying the judge over {len(labelset.labelled)} labelled pair(s)...\n")

    def show(pair, verdict):
        match = "[green]agree[/green]" if verdict.winner == pair.human else "[red]differ[/red]"
        flip = " [yellow](judge flipped on order)[/yellow]" if verdict.order_dependent else ""
        console.print(
            f"  {pair.company}: you {pair.human}, judge {verdict.winner} — {match}{flip}"
        )

    result, _ = score_against_judge(deps, labelset, on_pair=show)

    console.print(
        f"\n[bold]Agreement {result.rate:.0%}[/bold] ({result.matches}/{result.n}) — "
        f"[bold]{result.verdict}[/bold]"
    )
    if result.n < 10:
        console.print(
            f"[dim]Label at least {10 - result.n} more pair(s) before trusting this number.[/dim]"
        )
    return 0


def _status(args) -> int:
    labelset = LabelSet.load(args.labels)
    console.print(
        f"{len(labelset.pairs)} pair(s), {len(labelset.labelled)} labelled, "
        f"{len(labelset.pairs) - len(labelset.labelled)} remaining"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    return {"build": _build, "label": _label, "score": _score, "status": _status}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
