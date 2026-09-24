"""Planning Agent: turns findings into a report structure.

It decides the sections, what each one has to establish, and roughly how long
it should be — the contract the Writer Agent then fills in.
"""

from __future__ import annotations

import logging

from ..deps import Deps
from ..state import Outline, ReportState
from .base import ask, format_findings

log = logging.getLogger(__name__)

SYSTEM = (
    "You are an editorial lead who structures research reports. You design an "
    "outline that answers the reader's real questions in the order they ask them."
)

PROMPT = """Company: {company}
Reporting period: {quarter}
Extra focus from the requester: {focus}

Findings gathered by the research agent:
{findings}

Design the outline for a market research report. Requirements:
- 5 to 7 sections, opening with an executive summary and closing with outlook or
  recommendations.
- Every section must be supported by the findings above — do not plan a section
  the research cannot fill.
- For each section give the purpose (what the reader should take away) and 2-4
  key points drawn from the findings.
- Set target_words per section; the whole report should land near {words} words.
- Set `synthesises` to true for sections that summarise or draw conclusions from
  the others rather than covering their own material -- an executive summary, an
  outlook, a recommendations section. Those are written last, once the rest
  exist, so they can refer to them instead of repeating them."""


def make_planning_node(deps: Deps):
    """Build the graph node that plans the report."""

    def plan(state: ReportState) -> dict:
        outline = ask(
            deps.writer_llm,
            Outline,
            SYSTEM,
            PROMPT.format(
                company=state["company"],
                quarter=state["quarter"],
                focus=state.get("focus") or "(none)",
                findings=format_findings(state.get("findings", [])),
                words=1800,
            ),
            meter=deps.meter("Planning Agent"),
        )
        log.info("planning: %d sections", len(outline.sections))
        return {
            "outline": outline,
            "trace": [
                "planning: "
                + " | ".join(s.heading for s in outline.sections)
            ],
        }

    return plan
