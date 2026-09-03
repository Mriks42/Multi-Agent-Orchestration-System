"""LLM-as-judge: the quality signal the deterministic metrics cannot provide.

Metrics in `metrics.py` measure form and provenance. They cannot tell you
whether a section fulfils its purpose, whether the analysis is specific or
generic, or whether two sections repeat each other -- which is what a research
report is actually judged on. That needs a model.

A judge is only worth having if it is built against its own failure modes:

* **Verbosity bias** -- longer answers score higher. The rubric states outright
  that length is not quality, and `word_count` is reported alongside so a score
  that tracks length is visible rather than hidden.
* **Position bias** -- in a pairwise comparison the first option wins more
  often. Every comparison is run twice with the order swapped; a result that
  flips is recorded as a tie rather than a win.
* **Self-preference** -- a model rates text from its own family higher. Worth
  knowing here, since the judge and the writer are both OpenAI models; it is a
  reason to trust pairwise deltas over absolute scores.
* **Drift** -- the judge is pinned to an explicit model at temperature 0, so a
  score change means the reports changed, not the judge.

And the step that is usually skipped: `agreement()` scores the judge against
human labels. An unvalidated judge produces numbers that feel rigorous and mean
nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from ..deps import Deps
from .metrics import _body

log = logging.getLogger(__name__)

CRITERIA = {
    "grounding": "Every substantive claim is traceable to the cited evidence, and "
                 "uncertain claims are hedged rather than asserted.",
    "purpose_fit": "Each section does the job its heading promises, with no drift "
                   "into other sections' material.",
    "non_redundancy": "Sections do not restate each other. The same figure or point "
                      "is not re-explained in multiple places.",
    "specificity": "Concrete figures, dates and named entities rather than generic "
                   "business language that would fit any company.",
    "usefulness": "An analyst reading this would come away able to act, not merely "
                  "informed that the company exists.",
}

SCALE = """Score each criterion 1-5 against these anchors:
  1 = fails outright
  2 = weak, would need rewriting
  3 = acceptable but unremarkable
  4 = good, minor faults only
  5 = could go to a client unchanged

Length is NOT quality. A short report that does its job scores higher than a
long one that pads. Do not reward volume."""

SYSTEM = (
    "You are a demanding editor of market research. You judge against the rubric "
    "given, not against your own taste, and you justify each score in one sentence."
)

SCORE_PROMPT = """Report under review, for {company} ({quarter}):
---
{draft}
---

Rubric:
{criteria}

{scale}

Score every criterion and give a one-sentence reason for each."""

COMPARE_PROMPT = """Two market research reports on {company} ({quarter}).

=== REPORT A ===
{a}

=== REPORT B ===
{b}

Rubric:
{criteria}

{scale}

Which report is better overall? Answer "A", "B", or "tie" if they are genuinely
equivalent. Judge on the rubric only -- not on which is longer."""


class CriterionScore(BaseModel):
    criterion: str
    score: int = Field(ge=1, le=5)
    reason: str


class JudgeScore(BaseModel):
    """Absolute scores for one report."""

    scores: list[CriterionScore]
    overall: int = Field(ge=1, le=5, description="Holistic score, not an average")

    def by_criterion(self) -> dict[str, int]:
        return {s.criterion: s.score for s in self.scores}

    @property
    def mean(self) -> float:
        return round(sum(s.score for s in self.scores) / len(self.scores), 2) if self.scores else 0.0


class Verdict(BaseModel):
    """A pairwise comparison."""

    winner: Literal["A", "B", "tie"]
    reason: str


@dataclass
class Comparison:
    """One pairwise comparison, run in both orders to cancel position bias."""

    company: str
    forward: str = ""
    swapped: str = ""
    reason: str = ""
    error: str = ""

    @property
    def winner(self) -> str:
        """Consistent across both orderings, or a tie.

        `forward` is judged with A first; `swapped` with the same reports in the
        opposite order, so its verdict is inverted before comparing.
        """
        if self.error:
            return "error"
        inverted = {"A": "B", "B": "A", "tie": "tie"}[self.swapped]
        return self.forward if self.forward == inverted else "tie"

    @property
    def order_dependent(self) -> bool:
        """True when the judge flipped -- a direct read on its position bias."""
        if self.error:
            return False
        return self.forward != {"A": "B", "B": "A", "tie": "tie"}[self.swapped]


def _criteria_block() -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in CRITERIA.items())


def score_report(deps: Deps, state) -> JudgeScore:
    """Absolute rubric scores for one report."""
    from ..agents.base import ask

    return ask(
        deps.judge,
        JudgeScore,
        SYSTEM,
        SCORE_PROMPT.format(
            company=state.get("company", ""),
            quarter=state.get("quarter", ""),
            draft=_body(state.get("draft", "")),
            criteria=_criteria_block(),
            scale=SCALE,
        ),
    )


def compare_reports(deps: Deps, company: str, quarter: str, a: str, b: str) -> Comparison:
    """Compare two drafts in both orderings.

    Two calls rather than one, deliberately: a single ordering measures the
    judge's position bias as much as the reports.
    """
    from ..agents.base import ask

    result = Comparison(company=company)
    try:
        for label, (first, second) in (("forward", (a, b)), ("swapped", (b, a))):
            verdict = ask(
                deps.judge,
                Verdict,
                SYSTEM,
                COMPARE_PROMPT.format(
                    company=company, quarter=quarter, a=_body(first), b=_body(second),
                    criteria=_criteria_block(), scale=SCALE,
                ),
            )
            setattr(result, label, verdict.winner)
            if label == "forward":
                result.reason = verdict.reason
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("comparison failed for %s", company)

    if result.order_dependent:
        log.warning("judge flipped when order was swapped for %s -- scored as tie", company)
    return result


@dataclass
class Agreement:
    """How often the judge matches a human label."""

    n: int = 0
    matches: int = 0
    disagreements: list[dict] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return round(self.matches / self.n, 3) if self.n else 0.0

    @property
    def verdict(self) -> str:
        """A judge below chance-adjusted usefulness should not be trusted."""
        if self.n < 10:
            return "insufficient labels to say"
        if self.rate >= 0.8:
            return "usable"
        if self.rate >= 0.6:
            return "weak — treat scores as directional only"
        return "unusable — do not report these scores"


def agreement(judge_labels: dict[str, str], human_labels: dict[str, str]) -> Agreement:
    """Compare judge verdicts to human ones over the same items.

    Only items a human actually labelled are scored, so a partial labelling set
    is fine -- the point is to know whether the judge tracks a person, before
    any of its numbers are quoted.
    """
    result = Agreement()
    for item, human in human_labels.items():
        if item not in judge_labels:
            continue
        result.n += 1
        if judge_labels[item] == human:
            result.matches += 1
        else:
            result.disagreements.append(
                {"item": item, "judge": judge_labels[item], "human": human}
            )
    return result
