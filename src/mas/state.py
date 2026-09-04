"""The shared state that flows through the agent graph.

Every agent is a pure function of this state: it reads the keys it needs and
returns a partial update. LangGraph merges those updates, so no agent ever
mutates another agent's data in place.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class Source(BaseModel):
    """A single piece of evidence the Research Agent collected."""

    title: str = Field(description="Title of the source")
    url: str = Field(default="", description="URL, or empty for model-recalled knowledge")
    snippet: str = Field(default="", description="Relevant excerpt")


class Finding(BaseModel):
    """One researched claim, kept tied to the sources that support it."""

    claim: str = Field(description="A specific, verifiable statement about the company or market")
    topic: str = Field(description="Short topic label, e.g. 'competition' or 'financials'")
    confidence: Literal["high", "medium", "low"] = "medium"
    source_ids: list[int] = Field(
        default_factory=list, description="Indices into ReportState['sources']"
    )


SYNTHESISING_HEADINGS = (
    "executive summary", "summary", "overview", "outlook", "recommendation",
    "conclusion", "key takeaway", "what this means",
)


class Section(BaseModel):
    """One planned section of the report."""

    heading: str
    purpose: str = Field(description="What this section must establish for the reader")
    key_points: list[str] = Field(default_factory=list)
    target_words: int = 250
    synthesises: bool = Field(
        default=False,
        description=(
            "True when this section summarises or draws conclusions from the other "
            "sections rather than covering its own material. Such sections are "
            "written after the rest, so they can see what the others already said."
        ),
    )

    @property
    def is_synthesising(self) -> bool:
        """Model flag, with a heading fallback.

        The planner sets `synthesises`, but a missed flag on an Executive
        Summary is the exact case this exists to catch, so the heading is
        checked too rather than trusting the model alone.
        """
        if self.synthesises:
            return True
        lowered = self.heading.lower()
        return any(word in lowered for word in SYNTHESISING_HEADINGS)


class Outline(BaseModel):
    """The Planning Agent's report structure."""

    title: str
    audience: str = "executive stakeholders"
    sections: list[Section]


class Issue(BaseModel):
    """A single defect the Reviewer Agent found in the draft."""

    severity: Literal["blocker", "major", "minor"]
    section: str = Field(default="", description="Heading the issue belongs to, if section-specific")
    problem: str
    fix: str = Field(description="Concrete instruction the Writer Agent can act on")


class Review(BaseModel):
    """The Reviewer Agent's verdict on a draft."""

    approved: bool
    issues: list[Issue] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(
        default_factory=list, description="Statements in the draft no finding backs up"
    )
    summary: str = ""

    @property
    def blockers(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocker"]


def merge_sections(left: dict[str, str] | None, right: dict[str, str] | None) -> dict[str, str]:
    """Reducer for `sections`, written to by several branches at once.

    Parallel `write_section` branches each return a single-entry dict. Merging
    rather than replacing is what lets an untouched section survive a revision
    pass: only the branches that actually ran contribute keys.
    """
    return {**(left or {}), **(right or {})}


class ReportState(TypedDict, total=False):
    """State passed between every node in the graph."""

    # Inputs
    company: str
    quarter: str
    focus: str

    # Research Agent
    sources: list[Source]
    findings: list[Finding]

    # Planning Agent
    outline: Outline

    # Writer Agent
    sections: Annotated[dict[str, str], merge_sections]
    draft: str

    # Reviewer Agent
    review: Review
    revision: int
    max_revisions: int

    # Observability: `operator.add` makes this append-only across nodes.
    trace: Annotated[list[str], operator.add]


def initial_state(
    company: str, quarter: str, focus: str = "", max_revisions: int = 2
) -> ReportState:
    """Build the starting state for a run."""
    return ReportState(
        company=company,
        quarter=quarter,
        focus=focus,
        sources=[],
        findings=[],
        sections={},
        draft="",
        revision=0,
        max_revisions=max_revisions,
        trace=[],
    )
