"""Writer Agent: generates the report content, section by section.

On the first pass it drafts each section from the outline and findings. On a
revision pass it rewrites only the sections the Reviewer flagged, so an approved
section is never destabilised by an unrelated fix.
"""

from __future__ import annotations

import logging

from ..deps import Deps
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

Write the body of this section only. Do not repeat the heading, do not write an
introduction to the report, and do not cover other sections' material.

Provenance rules — these are not style preferences:
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


def make_writer_node(deps: Deps):
    """Build the graph node that writes and revises the report."""

    def write(state: ReportState) -> dict:
        outline = state["outline"]
        findings = format_findings(state.get("findings", []))
        sources = format_sources(state.get("sources", []))
        review = state.get("review")
        existing = dict(state.get("sections", {}))
        revision = state.get("revision", 0)

        written: dict[str, str] = {}
        for section in outline.sections:
            targeted = _issues_for(section.heading, review.issues) if review else []
            first_pass = section.heading not in existing

            if not first_pass and not targeted:
                written[section.heading] = existing[section.heading]  # already approved
                continue

            if first_pass:
                body = ask_text(
                    deps.writer_llm,
                    SYSTEM,
                    DRAFT_PROMPT.format(
                        title=outline.title,
                        audience=outline.audience,
                        company=state["company"],
                        quarter=state["quarter"],
                        heading=section.heading,
                        purpose=section.purpose,
                        key_points="\n".join(f"- {p}" for p in section.key_points) or "- (none)",
                        target_words=section.target_words,
                        findings=findings,
                        sources=sources,
                    ),
                )
            else:
                body = ask_text(
                    deps.writer_llm,
                    SYSTEM,
                    REVISE_PROMPT.format(
                        heading=section.heading,
                        purpose=section.purpose,
                        current=existing[section.heading],
                        findings=findings,
                        sources=sources,
                        issues="\n".join(
                            f"- [{i.severity}] {i.problem}\n  Fix: {i.fix}" for i in targeted
                        ),
                        target_words=section.target_words,
                    ),
                )
            written[section.heading] = body

        rewritten = sum(
            1 for h, b in written.items() if existing.get(h) != b and h in existing
        )
        log.info("writer: revision %d, %d sections written", revision, len(written))
        return {
            "sections": written,
            "draft": _render(outline, written, state.get("findings", [])),
            "revision": revision + 1,
            "trace": [
                f"writer: pass {revision + 1}, "
                + (f"{rewritten} section(s) revised" if existing else f"{len(written)} section(s) drafted")
            ],
        }

    return write
