"""Writer Agent: generates the report content, one section per branch.

Sections are independent LLM calls, so the graph fans them out to run
concurrently and merges the results through the `sections` reducer. Three parts:

  plan_sections  decides which sections need work this pass (no LLM)
  write_section  drafts or revises exactly one section (the parallel unit)
  assemble       renders the merged sections into the final document

On a revision pass only the flagged sections are dispatched, so an approved
section is never redrafted -- it simply survives in the merged state.
"""

from __future__ import annotations

import logging

from ..deps import Deps
from ..period import discipline
from ..state import Issue, ReportState
from .base import (
    REVIEW_HEADING,
    ask_text,
    format_findings,
    format_sources,
    provenance,
)

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a market research writer. You write in plain, concrete prose for "
    "executives: claims backed by evidence, numbers where you have them, and no "
    "filler. You never state a fact the findings do not support."
)

DRAFT_PROMPT = """Report: {title}
Audience: {audience}
Company: {company} | Period: {quarter}

Section to write: {heading}
Purpose: {purpose}
Key points to cover:
{key_points}
Target length: about {target_words} words.

Findings available (this is your only permitted source of fact):
{findings}

Sources, cite by index as [n] when you use one:
{sources}

Other sections already written, which you must not duplicate:
{siblings}

Write the body of this section only. Do not repeat the heading, do not write an
introduction to the report, and do not cover other sections' material.

{discipline}

Provenance rules - these are not style preferences:
- A finding marked UNSOURCED is the model's recollection, not verified fact.
  Never state its figures as established. Attribute them ("reportedly",
  "estimated at around") or leave them out. A precise unsourced number stated
  flatly is the worst failure this report can contain.
- A finding with sources may be stated directly; cite it as [n].
- If a key point has no supporting finding, note the gap in one short clause and
  move on. Do not invent detail to fill it, and do not pad the section with
  paragraphs about what you could not determine."""

REVISE_PROMPT = """You are revising one section of a market research report.

Section: {heading}
Purpose: {purpose}

Current text:
{current}

Findings available (your only permitted source of fact):
{findings}

Sources, cite by index as [n]:
{sources}

The reviewer raised these issues:
{issues}

Rewrite the section so every issue is resolved. Keep what already works, change
what was flagged, and stay near {target_words} words. Output the revised body
text only."""


def _render(outline, sections: dict[str, str], findings) -> str:
    """Assemble the section map into one markdown document.

    The provenance footer is not decoration: a reader who cannot see that a
    report rests on unsourced recall will read its figures as researched.
    """
    parts = [f"# {outline.title}", ""]
    for section in outline.sections:
        parts += [f"## {section.heading}", "", sections.get(section.heading, "").strip(), ""]

    sourced, total = provenance(findings)
    parts += ["---", "", "## Provenance", ""]
    if total == 0:
        parts.append("No research findings backed this report.")
    elif sourced == 0:
        parts.append(
            f"**None of the {total} findings behind this report are backed by a retrieved "
            "source.** Every figure is model recollection and must be verified before use."
        )
    else:
        unsourced = total - sourced
        parts.append(
            f"{sourced} of {total} findings are backed by a retrieved source; "
            + (
                "1 rests on model recollection and needs verification."
                if unsourced == 1
                else f"{unsourced} rest on model recollection and need verification."
            )
        )
    return "\n".join(parts).strip() + "\n"


_REVIEW_CAVEAT = (
    "The Reviewer checks this draft against the findings above, not against "
    "reality. A figure that research collected wrongly can still pass review."
)


def review_footer(review) -> str:
    """Render the review verdict for publication.

    The report ships whether or not the Reviewer signed off -- the revision
    budget runs out and the draft is published with its objections unresolved.
    Until this existed, none of that reached the artifact: the reader got a
    polished document whose Provenance footer said every finding was sourced,
    with no indication that the fact-checker had raised blockers against it.
    That is the project's own central failure one level up -- an artifact that
    reads as more trustworthy than it is -- so the verdict travels with the
    report rather than scrolling past in a log line.
    """
    parts = ["---", "", REVIEW_HEADING, ""]

    if review is None:
        parts += ["This report was not reviewed.", "", _REVIEW_CAVEAT]
        return "\n".join(parts)

    serious = [i for i in review.issues if i.severity in ("blocker", "major")]
    minor = len(review.issues) - len(serious)

    if review.approved:
        parts.append("**Approved.** The Reviewer raised no blocking or major issues.")
    else:
        blockers = sum(1 for i in serious if i.severity == "blocker")
        parts.append(
            f"**Published with {len(review.issues)} unresolved issue(s)** "
            f"({blockers} blocker, {len(serious) - blockers} major"
            + (f", {minor} minor" if minor else "")
            + "). The Reviewer did not sign off on this draft."
        )

    if serious:
        parts.append("")
        parts += [
            f"- **{i.severity}** — {i.section or 'report'}: {i.problem.strip()}"
            for i in serious
        ]

    parts += ["", _REVIEW_CAVEAT]
    return "\n".join(parts)


def stamp_review(draft: str, review) -> str:
    """Append the review verdict to a finished draft, exactly once.

    Applied after the graph rather than inside `assemble`, because assemble runs
    *before* the review that judges what it produced -- the final verdict does
    not exist until the run is over. It lands after the Provenance section so
    that `_body()` in the eval metrics continues to exclude it: the disclosure
    is apparatus, not report prose, and must not move a metric.
    """
    if not draft or REVIEW_HEADING in draft:
        return draft
    return draft.rstrip() + "\n\n" + review_footer(review) + "\n"


def _issues_for(heading: str, issues: list[Issue]) -> list[Issue]:
    """Issues naming this section, plus report-wide ones that name no section."""
    return [i for i in issues if i.section == heading or not i.section]


def _due_now(outline, existing: dict[str, str], review) -> list:
    """The sections to dispatch this round.

    Drafting runs in two waves. Body sections go first, all at once; sections
    that summarise the others go second, once there is something to summarise.
    Drafting everything simultaneously was cheaper by one round-trip but left
    the executive summary blind to the report it introduces, which is why the
    judge scored `non_redundancy` 4/5 on all twelve eval reports while every
    other criterion scored 5.

    A revision pass ignores waves entirely and dispatches whatever was flagged.
    """
    if review is not None:
        return [s for s in outline.sections if _issues_for(s.heading, review.issues)]

    body = [s for s in outline.sections if not s.is_synthesising]
    synthesis = [s for s in outline.sections if s.is_synthesising]

    pending_body = [s for s in body if s.heading not in existing]
    if pending_body:
        return pending_body
    # An outline of nothing but summaries would otherwise never dispatch.
    return [s for s in synthesis if s.heading not in existing] or [
        s for s in body if s.heading not in existing
    ]


def plan_sections(state: ReportState) -> list[dict]:
    """Build one task payload per section that needs writing this round.

    Returns [] when the draft is complete and nothing is flagged, which the
    graph reads as "nothing to dispatch".
    """
    outline = state["outline"]
    existing = state.get("sections", {})
    review = state.get("review")
    findings = format_findings(state.get("findings", []))
    sources = format_sources(state.get("sources", []))

    tasks: list[dict] = []
    for section in _due_now(outline, existing, review):
        targeted = _issues_for(section.heading, review.issues) if review else []

        tasks.append(
            {
                "heading": section.heading,
                "purpose": section.purpose,
                "key_points": section.key_points,
                "target_words": section.target_words,
                "title": outline.title,
                "audience": outline.audience,
                "company": state["company"],
                "quarter": state["quarter"],
                "findings": findings,
                "sources": sources,
                "current": existing.get(section.heading, ""),
                "discipline": discipline(state["company"], state["quarter"]),
                "issues": targeted,
                # Siblings are what stop a section repeating the others. Body
                # sections see none (they are drafted together); summarising
                # sections see all of them, which is the point of the wave.
                "siblings": {h: t for h, t in existing.items() if h != section.heading},
            }
        )
    return tasks


def make_write_section_node(deps: Deps):
    """Build the node that writes ONE section. This is the parallel unit."""

    def write_section(task: dict) -> dict:
        heading = task["heading"]
        if task["issues"]:
            body = ask_text(
                deps.writer_llm,
                SYSTEM,
                REVISE_PROMPT.format(
                    heading=heading,
                    purpose=task["purpose"],
                    current=task["current"],
                    findings=task["findings"],
                    sources=task["sources"],
                    issues="\n".join(
                        f"- [{i.severity}] {i.problem}\n  Fix: {i.fix}" for i in task["issues"]
                    ),
                    target_words=task["target_words"],
                ),
                meter=deps.meter("Writer Agent"),
            )
        else:
            siblings = task["siblings"]
            body = ask_text(
                deps.writer_llm,
                SYSTEM,
                DRAFT_PROMPT.format(
                    title=task["title"],
                    audience=task["audience"],
                    company=task["company"],
                    quarter=task["quarter"],
                    heading=heading,
                    purpose=task["purpose"],
                    key_points="\n".join(f"- {p}" for p in task["key_points"]) or "- (none)",
                    target_words=task["target_words"],
                    findings=task["findings"],
                    sources=task["sources"],
                    discipline=task.get("discipline", ""),
                    siblings="\n".join(f"### {h}\n{t[:400]}" for h, t in siblings.items())
                    or "(none yet - this pass drafts all sections together)",
                ),
                meter=deps.meter("Writer Agent"),
            )
        # A single-key dict: the `sections` reducer merges it with whatever the
        # branches running alongside this one return.
        return {"sections": {heading: body}}

    return write_section


def make_assemble_node(deps: Deps):
    """Build the node that renders merged sections once all branches finish."""

    def assemble(state: ReportState) -> dict:
        outline = state["outline"]
        sections = state.get("sections", {})
        revision = state.get("revision", 0)
        present = sum(1 for s in outline.sections if sections.get(s.heading))
        complete = present == len(outline.sections)

        # The revision budget counts finished drafts, not drafting rounds --
        # otherwise the two waves of a first draft would spend it before the
        # reviewer ever saw the report.
        if state.get("review") is not None:
            detail = f"{len(plan_sections(state))} of {present} section(s) revised"
        elif complete:
            detail = f"{present} section(s) drafted"
        else:
            # Names the second wave, so two "Writer Agent" rows in the UI read
            # as one draft in two stages rather than as a repeated step.
            remaining = len(outline.sections) - present
            detail = (
                f"{present} of {len(outline.sections)} section(s) drafted in parallel; "
                f"{remaining} summarising section(s) follow"
            )

        log.info("assemble: %s", detail)
        return {
            "draft": _render(outline, sections, state.get("findings", [])),
            "revision": revision + 1 if complete else revision,
            "trace": [
                (f"writer: pass {revision + 1}, " if complete else "writer: ") + detail
            ],
        }

    return assemble
