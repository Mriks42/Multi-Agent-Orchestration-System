"""Runs the eval suite and writes a comparable result file.

Results are written as JSON keyed by a run label so two runs can be diffed --
which is the point of the harness. A prompt change that raises the
sourced-finding rate but doubles unattributed figures is a regression, and only
a stored baseline makes that visible.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..deps import Deps
from ..graph import run_report
from .cases import Case
from .metrics import Metrics, aggregate, score_run
from .seeded import run_probes, score_probes

log = logging.getLogger(__name__)


def run_case(deps: Deps, case: Case, max_revisions: int | None = None) -> Metrics:
    """Run one report and score it. Failures become a scored row, not a crash."""
    started = time.time()
    try:
        state = run_report(
            deps,
            company=case.company,
            quarter=case.quarter,
            focus=case.focus,
            max_revisions=max_revisions,
        )
    except Exception as exc:
        log.exception("case %s failed", case.label)
        return Metrics(
            company=case.company,
            quarter=case.quarter,
            duration_s=round(time.time() - started, 1),
            error=f"{type(exc).__name__}: {exc}",
        )

    metrics = score_run(state, duration_s=time.time() - started)
    metrics.extra["coverage"] = case.coverage
    return metrics


def run_suite(
    deps: Deps,
    cases: list[Case],
    max_revisions: int | None = None,
    with_probes: bool = True,
    on_case: Callable[[Case, Metrics], None] | None = None,
) -> dict:
    """Run every case plus the seeded-error probes; return the full result."""
    started = time.time()
    runs: list[Metrics] = []

    for case in cases:
        metrics = run_case(deps, case, max_revisions=max_revisions)
        runs.append(metrics)
        if on_case:
            on_case(case, metrics)

    result = {
        "label": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "model": deps.settings.model,
        "reviewer_model": deps.settings.reviewer_model,
        "cases": [m.to_dict() for m in runs],
        "summary": aggregate(runs),
        "by_coverage": _by_coverage(runs),
        "total_duration_s": round(time.time() - started, 1),
    }

    if with_probes:
        probes = run_probes(deps)
        result["probes"] = [asdict(p) for p in probes]
        result["probe_summary"] = score_probes(probes)

    return result


def _by_coverage(runs: list[Metrics]) -> dict:
    """Break the headline metric out by expected evidence availability.

    The aggregate alone hides the interesting result: whether thin coverage
    produces honest hedging or confident invention.
    """
    groups: dict[str, list[Metrics]] = {}
    for m in runs:
        if not m.error:
            groups.setdefault(m.extra.get("coverage", "unknown"), []).append(m)

    return {
        coverage: {
            "cases": len(ms),
            "sourced_finding_rate": round(sum(m.sourced_finding_rate for m in ms) / len(ms), 3),
            "unattributed_figure_count": round(
                sum(m.unattributed_figure_count for m in ms) / len(ms), 2
            ),
            "citation_count": round(sum(m.citation_count for m in ms) / len(ms), 1),
        }
        for coverage, ms in sorted(groups.items())
    }


def save(result: dict, directory: Path) -> Path:
    """Write the result to `<directory>/<label>.json` and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"eval-{result['label']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path


def load_baseline(directory: Path) -> dict | None:
    """The most recent stored result, or None if this is the first run."""
    files = sorted(directory.glob("eval-*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def compare(current: dict, baseline: dict | None) -> list[dict]:
    """Diff headline metrics against a baseline.

    `better` encodes direction per metric, because a rise is an improvement for
    citations and a regression for unattributed figures.
    """
    if not baseline:
        return []

    higher_is_better = {
        "sourced_finding_rate": True,
        "cited_section_rate": True,
        "citation_count": True,
        "approval_rate": True,
        "unattributed_figure_count": False,
        "orphan_citation_count": False,
        "revisions_used": False,
    }

    rows = []
    for metric, higher in higher_is_better.items():
        now = current["summary"].get(metric, 0)
        was = baseline["summary"].get(metric, 0)
        delta = round(now - was, 3)
        rows.append(
            {
                "metric": metric,
                "baseline": was,
                "current": now,
                "delta": delta,
                "improved": None if delta == 0 else ((delta > 0) == higher),
            }
        )
    return rows
