"""Test doubles: a scripted chat model so the graph runs with no network."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from mas.config import Settings
from mas.deps import Deps
from mas.state import Finding, Outline, Review, Section, Source


class _Structured:
    """What `with_structured_output(schema)` returns: an object with .invoke().

    Honours `include_raw` the way ChatOpenAI does -- returning the parsed object
    wrapped beside the raw message -- because that wrapper is where token usage
    lives, and a fake that skipped it would leave the cost accounting untested.
    """

    def __init__(self, model: "FakeChatModel", schema: type[BaseModel], include_raw: bool = False):
        self.model = model
        self.schema = schema
        self.include_raw = include_raw

    def invoke(self, messages, **kwargs):
        self.model.calls.append((self.schema.__name__, messages))
        handler = self.model.handlers.get(self.schema.__name__)
        if handler is None:
            raise AssertionError(f"no scripted response for schema {self.schema.__name__}")
        parsed = handler(self.schema, messages, self.model)
        if not self.include_raw:
            return parsed
        return {"raw": self.model.message(""), "parsed": parsed, "parsing_error": None}


class _Message:
    def __init__(self, content: str, usage: tuple[int, int] = (0, 0), model: str = ""):
        self.content = content
        self.usage_metadata = {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "total_tokens": sum(usage),
        }
        self.response_metadata = {"model_name": model}


class FakeChatModel:
    """Scripted chat model.

    `handlers` maps a pydantic schema name to `fn(schema, messages, model)`.
    `text_handler` produces free-text replies for the Writer Agent.
    `usage` and `model_name` are what every reply reports spending, so a test
    can assert on the ledger without a network call.
    """

    def __init__(self, handlers=None, text_handler=None, usage=(100, 50),
                 model_name="gpt-4o-mini"):
        self.handlers = handlers or {}
        self.text_handler = text_handler or (lambda messages, model: "body text")
        self.calls: list[tuple[str, list]] = []
        self.usage = usage
        self.model_name = model_name

    def message(self, content: str) -> _Message:
        return _Message(content, usage=self.usage, model=self.model_name)

    def with_structured_output(self, schema, include_raw=False, **kwargs):
        return _Structured(self, schema, include_raw=include_raw)

    def invoke(self, messages, **kwargs):
        self.calls.append(("text", messages))
        return self.message(self.text_handler(messages, self))


def make_deps(writer: FakeChatModel, reviewer: FakeChatModel | None = None, search=None) -> Deps:
    """Assemble Deps from fakes."""
    return Deps(
        settings=Settings(max_revisions=2, search_backend="none"),
        writer_llm=writer,
        reviewer_llm=reviewer or writer,
        search=search if search is not None else (lambda q, n=5: []),
    )


def run_writer_pass(deps, state):
    """Drive one full writer pass the way the graph does: plan, fan out, assemble.

    Mirrors dispatch -> write_section (xN) -> assemble in a single call so tests
    can exercise the whole pass without standing up a graph.
    """
    from mas.agents.writer import make_assemble_node, make_write_section_node, plan_sections
    from mas.state import merge_sections

    tasks = plan_sections(state)
    write = make_write_section_node(deps)
    sections = dict(state.get("sections", {}))
    for task in tasks:
        sections = merge_sections(sections, write(task)["sections"])

    merged = {**state, "sections": sections}
    return {**merged, **make_assemble_node(deps)(merged)}


@pytest.fixture
def sample_outline() -> Outline:
    return Outline(
        title="Company X — Q4 Market Research",
        sections=[
            Section(heading="Executive Summary", purpose="Frame the quarter", target_words=150),
            Section(heading="Competitive Position", purpose="Compare rivals", target_words=300),
        ],
    )


@pytest.fixture
def sample_findings() -> list[Finding]:
    return [
        Finding(claim="Revenue grew 12% year over year.", topic="financials", source_ids=[0]),
        Finding(claim="Two new rivals entered the mid-market.", topic="competition"),
    ]


@pytest.fixture
def sample_sources() -> list[Source]:
    return [Source(title="Q4 earnings release", url="https://example.com/q4", snippet="...")]


@pytest.fixture
def approving_review() -> Review:
    return Review(approved=True, summary="Accurate and complete.")
