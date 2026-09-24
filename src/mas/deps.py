"""Dependency container handed to every agent factory.

Bundling the models and tools here is what keeps the agents free of global
state: `build_graph(deps)` in a test gets stubs, in production gets real ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings, load_settings
from .cost import Ledger
from .llm import ChatModel, build_chat_model
from .tools import SearchTool, build_search_tool


@dataclass
class Deps:
    settings: Settings
    writer_llm: ChatModel
    reviewer_llm: ChatModel
    search: SearchTool
    judge_llm: ChatModel | None = None
    """Built lazily by `judge` -- a report run never needs it."""

    broker: object | None = None
    """When set, section writing is farmed out to worker processes instead of
    running as in-process concurrent branches. Typed loosely to keep the
    distributed package an optional import."""

    ledger: Ledger = field(default_factory=Ledger)
    """Token and cost tally for this run. Injected like everything else, so a
    worker process gets its own and a test can read one."""

    def meter(self, agent: str):
        """A recorder for `ask`/`ask_text`, so agents never import Ledger."""
        return self.ledger.meter(agent)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, broker=None) -> "Deps":
        settings = settings or load_settings()
        return cls(
            settings=settings,
            writer_llm=build_chat_model(settings, "default"),
            reviewer_llm=build_chat_model(settings, "reviewer"),
            search=build_search_tool(settings.search_backend),
            broker=broker,
        )

    @property
    def judge(self) -> ChatModel:
        """The pinned judge model, built on first use."""
        if self.judge_llm is None:
            self.judge_llm = build_chat_model(self.settings, "judge")
        return self.judge_llm
