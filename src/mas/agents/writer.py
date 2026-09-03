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
from .base import ask_text, format_findings, format_sources, provenance

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


def _issues_for(heading: str, issues: list[Issue]) -> list[Issue]:
    """Issues naming this section, plus report-wide ones that name no section."""
    return [i for i in issues if i.section == heading or not i.section]


def plan_sections(state: ReportState) -> list[dict]:
    """Build one task payload per section that needs writing this pass.

    Returns [] when every section is already approved, which the graph reads as
    "nothing to dispatch".
    """
    outline = state["outline"]
    existing = state.get("sections", {})
    review = state.get("review")
    findings = format_findings(state.get("findings", []))
    sources = format_sources(state.get("sources", []))

    tasks: list[dict] = []
    for section in outline.sections:
        targeted = _issues_for(section.heading, review.issues) if review else []
        first_pass = section.heading not in existing
        if not first_pass and not targeted:
            continue  # already approved; the reducer keeps the existing text

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
                # Siblings let a section avoid repeating what others say. On the
                # first pass there are none, which is the accepted trade-off of
                # drafting every section at once instead of in sequence.
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

        # Report what this pass actually did, not how many sections exist. The
        # fan-out refactor lost that distinction, so a revision touching one
        # section still announced "6 section(s) assembled".
        dispatched = len(plan_sections(state))
        detail = (
            f"{present} section(s) drafted" if revision == 0
            else f"{dispatched} of {present} section(s) revised"
        )

        log.info("assemble: pass %d, %s", revision + 1, detail)
        return {
            "draft": _render(outline, sections, state.get("findings", [])),
            "revision": revision + 1,
            "trace": [f"writer: pass {revision + 1}, {detail}"],
        }

    return assemble
