"""The eval dataset.

Companies are chosen to spread the axis that actually moves the metrics:
whether public reporting exists for the Research Agent to find. A suite of only
well-covered mega-caps would report a flattering sourced-finding rate that says
nothing about how the system behaves when evidence is thin -- which is exactly
when it fabricates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    company: str
    quarter: str = "Q4 2025"
    focus: str = ""
    coverage: str = "high"
    """Expected evidence availability: high | medium | low.

    Not an assertion about the company -- a label for reading the results. A low
    sourced-finding rate is a red flag on a mega-cap and expected on a private
    firm.
    """

    @property
    def label(self) -> str:
        return f"{self.company} {self.quarter}"


CASES: list[Case] = [
    # Well-covered public companies: earnings are published and indexed.
    Case("NVIDIA", coverage="high"),
    Case("Microsoft", coverage="high"),
    Case("Shopify", coverage="high"),
    Case("Datadog", coverage="high"),
    Case("Snowflake", coverage="high"),
    Case("Airbnb", coverage="high"),
    # Public but less saturated coverage.
    Case("Zscaler", coverage="medium"),
    Case("Confluent", coverage="medium"),
    Case("Braze", coverage="medium"),
    # Private: no quarterly reporting exists, so honest output should be
    # heavily unsourced and hedged rather than confidently numeric.
    Case("Stripe", coverage="low"),
    Case("Databricks", coverage="low"),
    Case("Anthropic", coverage="low"),
]

SMOKE: list[Case] = [CASES[0], CASES[9]]
"""One high-coverage and one low-coverage case, for a fast check."""


def by_name(names: list[str]) -> list[Case]:
    """Select cases by company name, case-insensitively."""
    wanted = {n.lower() for n in names}
    found = [c for c in CASES if c.company.lower() in wanted]
    missing = wanted - {c.company.lower() for c in found}
    if missing:
        raise ValueError(f"unknown case(s): {sorted(missing)}")
    return found
