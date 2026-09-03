"""Runtime configuration, read from the environment or a local .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables for a run. Every field can be overridden by an env var."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MAS_", extra="ignore", protected_namespaces=()
    )

    # Cheap model for the high-volume drafting work, stronger one for review.
    model: str = "gpt-4o-mini"
    reviewer_model: str = "gpt-4o"
    temperature: float = 0.3

    max_revisions: int = 2
    """How many times the Writer may be sent back by the Reviewer before we ship."""

    search_backend: str = "duckduckgo"
    search_results: int = 5

    request_timeout: float = 90.0
    max_retries: int = 3


def load_settings(**overrides) -> Settings:
    """Load settings, applying explicit keyword overrides last."""
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
