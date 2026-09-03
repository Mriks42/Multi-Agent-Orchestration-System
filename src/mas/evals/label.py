"""Human labelling, so the judge can be checked against a person.

`agreement()` was unusable without this: there was no way to collect the labels
it compares against. This provides the two halves — a blind prompt that shows
you two reports and records which you prefer, and a scorer that replays the
judge over the same pairs.

Blind matters. If you can see which report the judge chose, your label stops
being independent and the agreement number measures nothing.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..deps import Deps
from .judge import Agreement, agreement, compare_reports

DEFAULT_PATH = Path("evals/labels.json")


@dataclass
class Pair:
    """Two reports on the same company, awaiting a human verdict."""

    item: str
    company: str
    quarter: str
    a: str
    b: str
    human: str = ""
    note: str = ""

    @property
    def labelled(self) -> bool:
        return self.human in ("A", "B", "tie")


@dataclass
class LabelSet:
    """Pairs plus whatever verdicts have been recorded so far."""

    pairs: list[Pair] = field(default_factory=list)

    @property
    def labelled(self) -> list[Pair]:
        return [p for p in self.pairs if p.labelled]

    def human_labels(self) -> dict[str, str]:
        return {p.item: p.human for p in self.labelled}

    def save(self, path: Path = DEFAULT_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pairs": [asdict(p) for p in self.pairs]}, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "LabelSet":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(pairs=[Pair(**p) for p in raw.get("pairs", [])])


def pairs_from_runs(runs: list[dict], seed: int = 0) -> list[Pair]:
    """Build labelling pairs from two or more stored eval results.

    Pairs are drawn from the same company across different runs, which is the
    comparison that matters: did this prompt change make the report better?
    A/B order is shuffled so the human is not always shown the newer one first.
    """
    rng = random.Random(seed)
    by_company: dict[str, list[tuple[str, str]]] = {}

    for run in runs:
        for label, draft in (run.get("drafts") or {}).items():
            if draft:
                by_company.setdefault(label, []).append((run["label"], draft))

    pairs: list[Pair] = []
    for label, versions in sorted(by_company.items()):
        for i in range(len(versions) - 1):
            (run_a, draft_a), (run_b, draft_b) = versions[i], versions[i + 1]
            if rng.random() < 0.5:  # hide which run is which
                (run_a, draft_a), (run_b, draft_b) = (run_b, draft_b), (run_a, draft_a)
            company, _, quarter = label.partition(" ")
            pairs.append(
                Pair(
                    item=f"{label}|{run_a}|{run_b}",
                    company=company,
                    quarter=quarter,
                    a=draft_a,
                    b=draft_b,
                )
            )
    return pairs


def score_against_judge(deps: Deps, labels: LabelSet, on_pair=None) -> tuple[Agreement, dict]:
    """Replay the judge over every human-labelled pair and score the match.

    Two judge calls per pair, since comparisons run in both orderings.
    """
    judge_labels: dict[str, str] = {}
    for pair in labels.labelled:
        verdict = compare_reports(deps, pair.company, pair.quarter, pair.a, pair.b)
        judge_labels[pair.item] = verdict.winner
        if on_pair:
            on_pair(pair, verdict)

    return agreement(judge_labels, labels.human_labels()), judge_labels
