"""Shared helpers for the agent nodes."""

from __future__ import annotations

import logging
import re
from typing import TypeVar

from pydantic import BaseModel

from ..llm import ChatModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _meter(meter, raw) -> None:
    """Hand one response's token usage to the meter, if anyone is counting.

    Never raises. A missing usage field means an un-costed call, which is worth
    far less than a run that dies while tallying what it spent.
    """
    if meter is None or raw is None:
        return
    try:
        usage = getattr(raw, "usage_metadata", None) or {}
        model = (getattr(raw, "response_metadata", None) or {}).get("model_name", "")
        meter(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    except Exception:  # pragma: no cover - accounting must not break a run
        log.debug("could not record token usage", exc_info=True)


def ask(llm: ChatModel, schema: type[T], system: str, user: str, meter=None) -> T:
    """Run one structured LLM call and get back a validated pydantic object.

    `include_raw` because the parsed object carries no token counts: the usage
    lives on the raw message underneath it, and dropping that is what made a
    run's cost unmeasurable. A backend that ignores the flag still returns the
    parsed object directly, which is why both shapes are handled.
    """
    structured = llm.with_structured_output(schema, include_raw=True)
    result = structured.invoke(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    if isinstance(result, dict) and "parsed" in result:
        if result.get("parsing_error"):
            # include_raw turns a parse failure into a field. Keep it fatal.
            raise result["parsing_error"]
        _meter(meter, result.get("raw"))
        return result["parsed"]
    return result


def ask_text(llm: ChatModel, system: str, user: str, meter=None) -> str:
    """Run one free-text LLM call and return the message content as a string."""
    result = llm.invoke(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    _meter(meter, result)
    content = getattr(result, "content", result)
    if isinstance(content, list):  # some providers return content blocks
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content).strip()


def format_sources(sources, limit: int | None = None) -> str:
    """Render sources as a numbered list the model can cite by index."""
    chosen = sources[:limit] if limit else sources
    if not chosen:
        return "(no external sources retrieved)"
    return "\n".join(
        f"[{i}] {s.title} — {s.url or 'no url'}\n    {s.snippet}" for i, s in enumerate(chosen)
    )


def format_findings(findings) -> str:
    """Render findings so downstream agents can see evidence and confidence.

    UNSOURCED is spelled out rather than implied: it is the single most
    important signal the Writer and Reviewer act on, and a marker they skim
    past is exactly how a recalled figure becomes a stated fact.
    """
    if not findings:
        return "(no findings)"
    return "\n".join(
        f"- ({f.confidence}) [{f.topic}] {f.claim} "
        + (f"sources={f.source_ids}" if f.source_ids else "**UNSOURCED - model recollection**")
        for f in findings
    )


def provenance(findings) -> tuple[int, int]:
    """Return (sourced, total) finding counts."""
    return sum(1 for f in findings if f.source_ids), len(findings)


PROVENANCE_HEADING = "## Provenance"
REVIEW_HEADING = "## Review status"

# Everything from the Provenance heading onward is generated from the state:
# the provenance counts, and the review verdict stamped on at publication.
_GENERATED_FOOTER = re.compile(r"\n-{3,}\s*\n+##\s*(?:Provenance|Review status)\b")


def report_body(draft: str) -> str:
    """The prose the Writer produced, without the generated footers.

    Anything that reads the draft as a report must read this instead. A live
    Confluent run had the Reviewer fact-check the Provenance footer and raise
    "10 of 10 findings are backed by a retrieved source" as an unsupported
    claim -- against the Recommendations section, which did not contain it.

    That objection is not merely wrong, it is unanswerable: the footer is
    regenerated from state on every assemble, so no revision the Writer makes
    can remove it. The Reviewer would raise it again on the next pass and
    every pass after, spending the revision budget on a sentence the Writer
    does not control.
    """
    return _GENERATED_FOOTER.split(draft or "")[0]
