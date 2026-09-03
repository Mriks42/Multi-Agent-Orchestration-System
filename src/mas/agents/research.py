"""Research Agent: collects information and searches sources.

Two steps: plan a handful of search queries, run them, then distil the raw hits
into `Finding` objects that stay linked to the sources supporting them.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from ..deps import Deps
from ..period import discipline
from ..state import Finding, ReportState, Source
from .base import ask, format_sources

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a market research analyst. You gather evidence before forming opinions, "
    "you distinguish fact from inference, and you never invent numbers."
)

QUERY_PROMPT = """Company: {company}
Reporting period: {quarter}
Extra focus from the requester: {focus}

Propose {n} distinct web search queries that together cover this company's
quarter: financial performance, product and strategy moves, competitive
position, market conditions, and risks. Keep each query short and specific."""

FINDINGS_PROMPT = """Company: {company}
Reporting period: {quarter}
Extra focus from the requester: {focus}

Search results:
{sources}

Extract the factual findings that a market research report on this company's
{quarter} would need. Rules:
- One specific, verifiable claim per finding — no vague generalities.
- Cite the source indices that support each claim in `source_ids`.
- If a claim comes from your own background knowledge and no source above backs
  it, leave `source_ids` empty and set confidence to "low".
- Prefer figures, dates and named entities over adjectives.

{discipline}

Return 8-15 findings."""


class _Queries(BaseModel):
    queries: list[str] = Field(description="Web search queries")


class _Findings(BaseModel):
    findings: list[Finding]


def make_research_node(deps: Deps):
    """Build the graph node that performs research."""

    def research(state: ReportState) -> dict:
        company, quarter = state["company"], state["quarter"]
        focus = state.get("focus") or "(none)"

        plan = ask(
            deps.writer_llm,
            _Queries,
            SYSTEM,
            QUERY_PROMPT.format(company=company, quarter=quarter, focus=focus, n=5),
        )

        # Searches are network-bound and independent, so run them together. The
        # results are collected in query order rather than completion order,
        # which keeps source indices - and therefore every citation - stable
        # across runs.
        with ThreadPoolExecutor(max_workers=len(plan.queries) or 1) as pool:
            batches = list(
                pool.map(
                    lambda q: deps.search(q, deps.settings.search_results), plan.queries
                )
            )

        sources: list[Source] = []
        seen: set[str] = set()
        for batch in batches:
            for source in batch:
                key = source.url or source.title
                if key not in seen:
                    seen.add(key)
                    sources.append(source)

        result = ask(
            deps.writer_llm,
            _Findings,
            SYSTEM,
            FINDINGS_PROMPT.format(
                company=company,
                quarter=quarter,
                focus=focus,
                sources=format_sources(sources),
                discipline=discipline(company, quarter),
            ),
        )

        # Drop citations pointing past the end of the source list, so no later
        # agent can render a footnote that does not exist.
        findings = []
        for finding in result.findings:
            finding.source_ids = [i for i in finding.source_ids if 0 <= i < len(sources)]
            findings.append(finding)

        log.info("research: %d queries, %d sources, %d findings",
                 len(plan.queries), len(sources), len(findings))
        return {
            "sources": sources,
            "findings": findings,
            "trace": [f"research: {len(sources)} sources -> {len(findings)} findings"],
        }

    return research
