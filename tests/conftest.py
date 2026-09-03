"""Test doubles: a scripted chat model so the graph runs with no network."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from mas.config import Settings
from mas.deps import Deps
from mas.state import Finding, Outline, Review, Section, Source


class _Structured:
    """What `with_structured_output(schema)` returns: an object with .invoke()."""

    def __init__(self, model: "FakeChatModel", schema: type[BaseModel]):
        self.model = model
        self.schema = schema

    def invoke(self, messages, **kwargs):
        self.model.calls.append((self.schema.__name__, messages))
        handler = self.model.handlers.get(self.schema.__name__)
        if handler is None:
            raise AssertionError(f"no scripted response for schema {self.schema.__name__}")
        return handler(self.schema, messages, self.model)


class _Message:
    def __init__(self, content: str):
        self.content = content


class FakeChatModel:
    """Scripted chat model.

    `handlers` maps a pydantic schema name to `fn(schema, messages, model)`.
    `text_handler` produces free-text replies for the Writer Agent.
    """

    def __init__(self, handlers=None, text_handler=None):
        self.handlers = handlers or {}
        self.text_handler = text_handler or (lambda messages, model: "body text")
        self.calls: list[tuple[str, list]] = []

    def with_structured_output(self, schema, **kwargs):
        return _Structured(self, schema)

    def invoke(self, messages, **kwargs):
        self.calls.append(("text", messages))
        return _Message(self.text_handler(messages, self))


def make_deps(writer: FakeChatModel, reviewer: FakeChatModel | None = None, search=None) -> Deps:
    """Assemble Deps from fakes."""
    return Deps(
        settings=Settings(max_revisions=2, search_backend="none"),
        writer_llm=writer,
        reviewer_llm=reviewer or writer,
        search=search if search is not None else (lambda q, n=5: []),
    )


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
