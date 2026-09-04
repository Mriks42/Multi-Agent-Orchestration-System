"""Ablation: hold the company fixed and vary the system instead.

Every case in `cases.py` differs by company, which is uncontrolled -- NVIDIA and
Braze differ for a hundred reasons at once. That is fine for coverage but it
cannot answer "did this change help?", and it left a real question open: the
judge gave the same score to all twelve reports, and there was no way to tell
whether the judge was inert or the reports were genuinely alike.

So this runs one company under several configurations. `--no-search` is the
point of the exercise: it produces a wholly fabricated report, which we already
know from a live run. A measurement that cannot separate that from a properly
researched one is not measuring quality, and this is what says so.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings, load_settings
from ..deps import Deps
from ..graph import run_report
from .metrics import Metrics, score_run

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Variant:
    """One configuration to run the same company under."""

    name: str
    why: str
    settings: dict = field(default_factory=dict)
    max_revisions: int | None = None
    expect: str = ""
    """What we expect, so a surprising result is visible rather than absorbed."""


VARIANTS: list[Variant] = [
    Variant(
        name="baseline",
        why="current defaults",
        expect="the reference every other variant is read against",
    ),
    Variant(
        name="no-search",
        why="web search disabled; the model works from recollection alone",
        settings={"search_backend": "none"},
        expect="MUCH worse — no sources, so every figure is unverified. "
               "A measure that misses this is not measuring quality.",
    ),
    Variant(
        name="strong-writer",
        why="gpt-4o drafts instead of gpt-4o-mini, at roughly 15x the token cost",
        settings={"model": "gpt-4o"},
        expect="better, if paying for the stronger model is worth it",
    ),
    Variant(
        name="more-revisions",
        why="three reviewer passes instead of one",
        settings={},
        max_revisions=3,
        expect="fewer open issues; possibly better grounding",
    ),
]


@dataclass
class AblationRun:
    variant: Variant
    metrics: Metrics
    judge: dict = field(default_factory=dict)
    draft: str = ""


def run_variant(
    variant: Variant, company: str, quarter: str, base: Settings, judge: bool = True
) -> AblationRun:
    """Run one company under one configuration and score it."""
    settings = load_settings(**{**base.model_dump(), **variant.settings})
    started = time.time()

    try:
        deps = Deps.from_settings(settings)
        state = run_report(
            deps, company=company, quarter=quarter,
            max_revisions=variant.max_revisions if variant.max_revisions is not None else 1,
        )
    except Exception as exc:
        log.exception("variant %s failed", variant.name)
        return AblationRun(
            variant=variant,
            metrics=Metrics(company=company, quarter=quarter,
                            error=f"{type(exc).__name__}: {exc}"),
        )

    metrics = score_run(state, duration_s=time.time() - started)
    result = AblationRun(variant=variant, metrics=metrics, draft=state.get("draft", ""))

    if judge:
        from .judge import score_report

        try:
            verdict = score_report(deps, state)
            result.judge = {"overall": verdict.overall, **verdict.by_criterion()}
        except Exception as exc:
            log.warning("judge failed on %s: %s", variant.name, exc)

    return result


def run_ablation(
    company: str,
    quarter: str,
    variants: list[Variant] | None = None,
    judge: bool = True,
    on_variant: Callable[[AblationRun], None] | None = None,
) -> list[AblationRun]:
    """Run every variant against the same company."""
    base = load_settings()
    runs = []
    for variant in variants if variants is not None else VARIANTS:
        run = run_variant(variant, company, quarter, base, judge=judge)
        runs.append(run)
        if on_variant:
            on_variant(run)
    return runs


# Which direction the known-bad variant should move each measure. A measure
# that moves the wrong way is worse than one that does not move at all: it
# actively endorses the fabricated report.
WORSE_IS = {
    "sourced_finding_rate": "lower",
    "citation_count": "lower",
    "cited_section_rate": "lower",
    "unattributed_figure_count": "higher",
    "grounding": "lower",
    "specificity": "lower",
    "usefulness": "lower",
    "overall": "lower",
    "non_redundancy": "lower",
    "purpose_fit": "lower",
}


def discriminates(runs: list[AblationRun], key: str) -> dict:
    """Does this measure correctly separate the known-bad variant from baseline?

    The one question the ablation exists to answer. `no-search` is known to
    produce a fabricated report, so there are three outcomes and they are not
    equally bad:

      correct  -- moves the way a fabricated report should move it
      blind    -- does not move at all
      inverted -- moves the wrong way, scoring the fabrication *better*

    An inverted measure is the dangerous one. A blind measure tells you
    nothing; an inverted one tells you something false.
    """
    def value(name: str):
        for r in runs:
            if r.variant.name == name and not r.metrics.error:
                if key in r.judge:
                    return r.judge[key]
                return getattr(r.metrics, key, None)
        return None

    baseline, bad = value("baseline"), value("no-search")
    if baseline is None or bad is None:
        return {"measure": key, "verdict": "not run"}

    delta = round(bad - baseline, 3)
    expected = WORSE_IS.get(key, "lower")
    if delta == 0:
        verdict = "blind"
    elif (delta < 0) == (expected == "lower"):
        verdict = "correct"
    else:
        verdict = "inverted"

    return {
        "measure": key,
        "baseline": baseline,
        "no_search": bad,
        "delta": delta,
        "verdict": verdict,
        "separates": verdict == "correct",
    }
