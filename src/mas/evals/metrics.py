"""Metrics computed from a finished run.

Deliberately deterministic: every metric here is a function of the final state,
with no model in the loop. An LLM judge would be the obvious alternative, but a
judge that drifts between runs cannot tell you whether a prompt change helped --
which is the entire reason to have evals. The trade-off is that these measure
form and provenance rather than prose quality; `reviewer_catch_rate` in
`seeded.py` covers the one judgement that matters most.

Every metric returns a float in [0, 1] where higher is better, except the
`*_count` fields which are raw counts and lower is better.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# A figure stated as established fact: "revenue was $3 billion", "rose 20%".
# Attribution verbs ("reportedly", "estimated at") must NOT precede it.
_FIGURE = re.compile(
    r"\b(?:was|were|is|are|reached|totall?ed|hit|rose to|grew to|stands? at|"
    r"posted|reported(?! ly))\s+"
    r"(?:\$|€|£)?\d[\d,.]*\s*(?:%|percent|billion|million|trillion|bn|m\b)?",
    re.IGNORECASE,
)

_ATTRIBUTED = re.compile(
    r"\b(reportedly|estimated|approximately|around|roughly|about|"
    r"recollection|unverified|unconfirmed|allegedly|said to be)\b",
    re.IGNORECASE,
)

_CITATION = re.compile(r"\[(\d+)\]")
_HEADING = re.compile(r"^## (.+)$", re.MULTILINE)


@dataclass
class Metrics:
    """One run's scores. Serialised straight to JSON for the eval report."""

    company: str = ""
    quarter: str = ""

    # Provenance -- the project's central concern
    sourced_finding_rate: float = 0.0
    orphan_citation_count: int = 0
    unattributed_figure_count: int = 0
    provenance_footer: bool = False

    # Grounding
    citation_count: int = 0
    cited_section_rate: float = 0.0
    distinct_sources_cited: int = 0

    # Process
    approved: bool = False
    revisions_used: int = 0
    open_issue_count: int = 0
    blocker_count: int = 0

    # Shape
    section_count: int = 0
    word_count: int = 0

    # Cost
    duration_s: float = 0.0
    error: str = ""

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _body(draft: str) -> str:
    """The report without its Provenance footer, which is generated, not written."""
    return draft.split("\n---\n\n## Provenance")[0]


def sourced_finding_rate(findings) -> float:
    """Share of findings backed by a retrieved source. The headline metric."""
    if not findings:
        return 0.0
    return sum(1 for f in findings if f.source_ids) / len(findings)


def orphan_citations(draft: str, sources) -> int:
    """Citations pointing past the end of the source list. Should always be 0."""
    return sum(1 for m in _CITATION.finditer(draft) if int(m.group(1)) >= len(sources))


def unattributed_figures(draft: str) -> int:
    """Figures stated as established fact without an attribution hedge nearby.

    A heuristic, not a parser: it counts sentences containing a bare figure and
    no attribution word. Useful as a trend across runs rather than an absolute.
    """
    count = 0
    for sentence in re.split(r"(?<=[.!?])\s+", _body(draft)):
        if _FIGURE.search(sentence) and not _ATTRIBUTED.search(sentence):
            count += 1
    return count


def cited_section_rate(draft: str) -> float:
    """Share of sections carrying at least one citation."""
    body = _body(draft)
    blocks = re.split(_HEADING, body)[1:]  # [heading, text, heading, text, ...]
    sections = list(zip(blocks[::2], blocks[1::2]))
    if not sections:
        return 0.0
    return sum(1 for _, text in sections if _CITATION.search(text)) / len(sections)


def score_run(state, duration_s: float = 0.0, error: str = "") -> Metrics:
    """Reduce a finished ReportState to its metrics."""
    draft = state.get("draft", "") or ""
    findings = state.get("findings", []) or []
    sources = state.get("sources", []) or []
    review = state.get("review")
    body = _body(draft)

    cited = {int(m.group(1)) for m in _CITATION.finditer(draft)}

    return Metrics(
        company=state.get("company", ""),
        quarter=state.get("quarter", ""),
        sourced_finding_rate=round(sourced_finding_rate(findings), 3),
        orphan_citation_count=orphan_citations(draft, sources),
        unattributed_figure_count=unattributed_figures(draft),
        provenance_footer="## Provenance" in draft,
        citation_count=len(_CITATION.findall(draft)),
        cited_section_rate=round(cited_section_rate(draft), 3),
        distinct_sources_cited=len(cited),
        approved=bool(review and review.approved),
        revisions_used=state.get("revision", 0),
        open_issue_count=len(review.issues) if review and not review.approved else 0,
        blocker_count=len(review.blockers) if review else 0,
        section_count=len(_HEADING.findall(body)),
        word_count=len(body.split()),
        duration_s=round(duration_s, 1),
        error=error,
    )


def aggregate(runs: list[Metrics]) -> dict[str, Any]:
    """Summarise a suite. Failed runs are counted, never silently averaged away."""
    ok = [m for m in runs if not m.error]
    failed = [m for m in runs if m.error]

    def mean(attr: str) -> float:
        return round(sum(getattr(m, attr) for m in ok) / len(ok), 3) if ok else 0.0

    return {
        "runs": len(runs),
        "succeeded": len(ok),
        "failed": len(failed),
        "failures": [{"company": m.company, "error": m.error} for m in failed],
        "sourced_finding_rate": mean("sourced_finding_rate"),
        "cited_section_rate": mean("cited_section_rate"),
        "citation_count": mean("citation_count"),
        "unattributed_figure_count": mean("unattributed_figure_count"),
        "orphan_citation_count": sum(m.orphan_citation_count for m in ok),
        "approval_rate": round(sum(1 for m in ok if m.approved) / len(ok), 3) if ok else 0.0,
        "revisions_used": mean("revisions_used"),
        "word_count": mean("word_count"),
        "duration_s": mean("duration_s"),
    }
