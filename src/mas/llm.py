"""LLM access, isolated behind one factory so the graph can be tested offline.

Agents never import `ChatOpenAI` directly. They receive a chat model through
`Deps`, which means tests can hand them a stub with no network access.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .config import Settings


@runtime_checkable
class ChatModel(Protocol):
    """The slice of the LangChain chat-model interface the agents actually use."""

    def invoke(self, input, **kwargs): ...

    def with_structured_output(self, schema: type[BaseModel], **kwargs): ...


class MissingAPIKey(RuntimeError):
    """Raised when a real model is requested without credentials configured."""


def build_chat_model(settings: Settings, role: str = "default") -> ChatModel:
    """Return a chat model for `role`.

    'reviewer' gets the stronger model; 'judge' gets a pinned snapshot at
    temperature 0 so eval scores move only when the reports do.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise MissingAPIKey(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    from langchain_openai import ChatOpenAI  # imported lazily: keeps tests import-light

    model, temperature = settings.model, settings.temperature
    if role == "reviewer":
        model = settings.reviewer_model
    elif role == "judge":
        model, temperature = settings.judge_model, settings.judge_temperature

    return ChatOpenAI(
        model=model,
        temperature=temperature,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )
