"""Reviewer Agent: fact-checks the draft and decides whether it ships.

It gets the findings as ground truth and the draft as the thing under test. Its
verdict drives the one cycle in the graph: not approved sends the draft back to
the Writer with actionable fixes attached.
"""

from __future__ import annotations

import logging
import re

from ..deps import Deps
from ..state import Issue, ReportState, Review
from .base import ask, format_findings, format_sources, report_body
from .figures import check_figures

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a fact-checker and editor. You are adversarial about evidence: a "
    "statement is unsupported unless the findings back it. You are specific about "
    "fixes so the writer can act on them without guessing."
)

PROMPT = """Company: {company} | Period: {quarter}

Ground truth — the findings the research agent gathered. Nothing outside this
list counts as established fact:
{findings}

Sources behind those findings:
{sources}

Draft report under review:
---
{draft}
---

Review the draft and report back.
1. Flag every statement that the findings do not support, especially invented
   figures, dates, percentages and named entities. List them in
   `unsupported_claims` and raise a matching issue.
1a. A finding marked UNSOURCED is model recollection, NOT established fact. If
   the draft states an unsourced figure flatly ("revenue was $3 billion")
   instead of attributing it ("reportedly around $3 billion"), that is a
   blocker. Check every number in the draft against this rule.
1b. A sentence that openly says information is unavailable or was not disclosed
   is honest reporting, NOT an unsupported claim. Never flag it as one. Raise a
   minor issue only if such hedging has grown long enough to pad the section.
1c. Check that every figure belongs to {quarter}. Sources often report adjacent
   quarters, trailing-twelve-month totals or full years. A figure from another
   period presented as this one's is a blocker, and conflicting values for the
   same metric usually mean a period mismatch rather than disagreeing sources.
2. Raise issues for structural problems too: a section that misses its purpose,
   contradictions between sections, or padding with no content.
3. Severity: "blocker" for a factual error or fabrication, "major" for a real
   gap in substance, "minor" for style. Set `section` to the exact heading when
   the issue belongs to one section.
4. Every issue needs a `fix` the writer can execute directly.
5. Set `approved` to true only if there are no blocker or major issues.

This is revision {revision} of at most {max_revisions}."""


_CITATION = re.compile(r"\[(\d+)\]")


def check_citations(draft: str, sources) -> list[Issue]:
    """Find citations pointing past the end of the source list.

    A deterministic check, because the model is unreliable at it: an eval probe
    planting `[47]` against two sources passed review untouched. Anything code
    can verify exactly should not be delegated to a model that verifies it
    sometimes -- the LLM judges meaning, arithmetic like this is checked here.
    """
    orphans = sorted({int(m.group(1)) for m in _CITATION.finditer(draft)
                      if int(m.group(1)) >= len(sources)})
    return [
        Issue(
            severity="blocker",
            section="",
            problem=f"Citation [{n}] refers to a source that does not exist "
                    f"(only {len(sources)} source(s) were retrieved).",
            fix=f"Remove [{n}] or replace it with a valid source index "
                f"(0-{len(sources) - 1})." if sources else f"Remove [{n}]; no sources were retrieved.",
        )
        for n in orphans
    ]


def make_reviewer_node(deps: Deps):
    """Build the graph node that reviews the draft."""

    def review(state: ReportState) -> dict:
        # The generated footers are stripped first. They are computed from the
        # state, so reviewing them produces objections the Writer cannot act on
        # -- see `report_body`.
        draft = report_body(state.get("draft", ""))

        verdict = ask(
            deps.reviewer_llm,
            Review,
            SYSTEM,
            PROMPT.format(
                company=state["company"],
                quarter=state["quarter"],
                findings=format_findings(state.get("findings", [])),
                sources=format_sources(state.get("sources", [])),
                draft=draft,
                revision=state.get("revision", 1),
                max_revisions=state.get("max_revisions", 2),
            ),
            meter=deps.meter("Reviewer Agent"),
        )

        # Mechanical checks the model is unreliable at, merged into its verdict.
        orphans = check_citations(draft, state.get("sources", []))
        if orphans:
            verdict.issues = list(verdict.issues) + orphans
            log.info("reviewer: %d orphan citation(s) caught mechanically", len(orphans))

        ungrounded = check_figures(draft, state.get("findings", []), state.get("sources", []))
        if ungrounded:
            verdict.issues = list(verdict.issues) + ungrounded
            log.info("reviewer: %d ungrounded figure(s) caught mechanically", len(ungrounded))

        # The model is asked to be consistent here, but the graph's exit
        # condition depends on it, so enforce it rather than trusting it.
        if verdict.approved and any(
            i.severity in ("blocker", "major") for i in verdict.issues
        ):
            verdict.approved = False

        log.info(
            "reviewer: approved=%s, %d issues, %d unsupported claims",
            verdict.approved, len(verdict.issues), len(verdict.unsupported_claims),
        )
        return {
            "review": verdict,
            "trace": [
                f"reviewer: {'approved' if verdict.approved else 'changes requested'} "
                f"({len(verdict.issues)} issue(s), {len(verdict.unsupported_claims)} unsupported)"
            ],
        }

    return review
