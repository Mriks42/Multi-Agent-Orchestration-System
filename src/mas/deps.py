"""Dependency container handed to every agent factory.

Bundling the models and tools here is what keeps the agents free of global
state: `build_graph(deps)` in a test gets stubs, in production gets real ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, load_settings
from .llm import ChatModel, build_chat_model
from .tools import SearchTool, build_search_tool


@dataclass
class Deps:
    settings: Settings
    writer_llm: ChatModel
    reviewer_llm: ChatModel
    search: SearchTool

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Deps":
        settings = settings or load_settings()
        return cls(
            settings=settings,
            writer_llm=build_chat_model(settings, "default"),
            reviewer_llm=build_chat_model(settings, "reviewer"),
            search=build_search_tool(settings.search_backend),
        )
