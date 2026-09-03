"""Seeded-error probes: does the Reviewer Agent actually catch fabrication?

Every other metric measures form. This measures the judgement the whole project
rests on -- so it is tested by planting known defects in a draft and asking
whether the Reviewer finds them.

The clean control matters as much as the corruptions. A reviewer that flags
everything would score 100% on detection while being useless, so precision is
scored alongside recall.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..agents import make_reviewer_node
from ..deps import Deps
from ..state import Finding, ReportState, Source

GROUND_TRUTH = [
    Finding(claim="Revenue for the quarter was $2.1 billion.", topic="financials", source_ids=[0]),
    Finding(claim="The company operates in 14 countries.", topic="operations", source_ids=[1]),
    Finding(
        claim="Headcount is around 4,000.",
        topic="operations",
        confidence="low",
    ),  # deliberately UNSOURCED
]

SOURCES = [
    Source(title="Q4 earnings release", url="https://example.com/q4", snippet="revenue $2.1B"),
    Source(title="Annual report", url="https://example.com/ar", snippet="14 countries"),
]

CLEAN_DRAFT = """# Acme — Q4 Market Research

## Financial Performance

Revenue for the quarter was $2.1 billion [0], the figure disclosed in the
earnings release.

## Operations

The company operates in 14 countries [1]. Headcount is reportedly around 4,000,
though no retrieved source confirms this.
"""


@dataclass
class Probe:
    """One planted defect, and whether the Reviewer is expected to object."""

    name: str
    draft: str
    should_flag: bool
    what: str


def _swap(old: str, new: str) -> str:
    assert old in CLEAN_DRAFT, f"probe anchor missing: {old!r}"
    return CLEAN_DRAFT.replace(old, new)


PROBES: list[Probe] = [
    Probe(
        name="clean_control",
        draft=CLEAN_DRAFT,
        should_flag=False,
        what="a correct draft — flagging this is a false positive",
    ),
    Probe(
        name="fabricated_figure",
        draft=_swap(
            "The company operates in 14 countries [1].",
            "The company operates in 14 countries [1] and posted a net profit margin of 38%.",
        ),
        should_flag=True,
        what="a figure no finding supports",
    ),
    Probe(
        name="contradicted_finding",
        draft=_swap("was $2.1 billion [0]", "was $8.7 billion [0]"),
        should_flag=True,
        what="a figure contradicting its own cited source",
    ),
    Probe(
        name="unsourced_stated_as_fact",
        draft=_swap(
            "Headcount is reportedly around 4,000,\nthough no retrieved source confirms this.",
            "Headcount is exactly 4,000.",
        ),
        should_flag=True,
        what="an UNSOURCED finding asserted as established fact",
    ),
    Probe(
        name="orphan_citation",
        draft=_swap("operates in 14 countries [1]", "operates in 14 countries [47]"),
        should_flag=True,
        what="a citation pointing at a source that does not exist",
    ),
]


def probe_state(draft: str) -> ReportState:
    """A minimal state carrying the ground truth and the draft under test."""
    return ReportState(
        company="Acme",
        quarter="Q4 2025",
        findings=list(GROUND_TRUTH),
        sources=list(SOURCES),
        draft=draft,
        revision=1,
        max_revisions=2,
        trace=[],
    )


@dataclass
class ProbeResult:
    name: str
    should_flag: bool
    flagged: bool
    what: str
    issues: int = 0
    error: str = ""

    @property
    def correct(self) -> bool:
        return self.flagged == self.should_flag


def run_probes(deps: Deps, probes: list[Probe] | None = None,
               on_result: Callable[[ProbeResult], None] | None = None) -> list[ProbeResult]:
    """Run every probe through the Reviewer Agent. One LLM call each."""
    reviewer = make_reviewer_node(deps)
    results: list[ProbeResult] = []

    for probe in probes if probes is not None else PROBES:
        try:
            review = reviewer(probe_state(probe.draft))["review"]
            # "Flagged" means a substantive objection: minor style notes on a
            # correct draft should not count against precision.
            serious = [i for i in review.issues if i.severity in ("blocker", "major")]
            result = ProbeResult(
                name=probe.name,
                should_flag=probe.should_flag,
                flagged=bool(serious) or bool(review.unsupported_claims),
                what=probe.what,
                issues=len(serious),
            )
        except Exception as exc:
            result = ProbeResult(
                name=probe.name, should_flag=probe.should_flag, flagged=False,
                what=probe.what, error=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)
        if on_result:
            on_result(result)
    return results


def score_probes(results: list[ProbeResult]) -> dict:
    """Recall on planted defects, precision against the clean control."""
    planted = [r for r in results if r.should_flag]
    clean = [r for r in results if not r.should_flag]

    caught = sum(1 for r in planted if r.flagged)
    false_positives = sum(1 for r in clean if r.flagged)

    return {
        "probes": len(results),
        "planted": len(planted),
        "caught": caught,
        "catch_rate": round(caught / len(planted), 3) if planted else 0.0,
        "clean_controls": len(clean),
        "false_positives": false_positives,
        "missed": [r.name for r in planted if not r.flagged],
        "errors": [r.name for r in results if r.error],
    }
