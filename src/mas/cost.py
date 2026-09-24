"""What a run actually cost, per agent.

"What does a report cost?" could only ever be estimated here: the call count was
known, the token count was not. This measures both, per agent, so the answer is
read off a run rather than guessed from the pipeline diagram.

Two design choices worth knowing.

**An unpriced model reports no cost, not zero cost.** Model names change and
this table will go stale; a run using a model that is not in it reports its
tokens and says the cost is unknown. Silently charging $0.00 would be a measure
that moves the wrong way -- the thing `mas-ablate` exists to avoid.

**Only the orchestrator's own calls are counted.** In `--distributed` mode the
section drafting happens in `mas-worker` processes with their own Ledger, so a
distributed run under-reports. The totals say so rather than pretending.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass

# USD per 1,000,000 tokens, as (input, output).
#
# Verified against developers.openai.com/api/docs/pricing on 2026-09-24 rather
# than recalled: this file has the same failure mode as the hosting advice and
# the fiscal calendar, both of which were wrong from memory. Dated snapshots
# ("gpt-4o-2024-11-20") are not listed separately and price as their base model,
# which is what the prefix match below is for.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

PRICES_VERIFIED = "2026-09-24"


def price_for(model: str) -> tuple[float, float] | None:
    """Rates for a model, or None if this table has never heard of it.

    Longest prefix wins, so "gpt-4o-mini-2024-07-18" matches gpt-4o-mini and not
    the much more expensive gpt-4o.
    """
    matches = [name for name in PRICES if model.startswith(name)]
    return PRICES[max(matches, key=len)] if matches else None


@dataclass(frozen=True)
class Spend:
    """One agent's usage, or a whole run's."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True
    """False when some model here is missing from PRICES, so cost_usd is a
    floor rather than the answer."""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Spend") -> "Spend":
        return Spend(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            priced=self.priced and other.priced,
        )


class Ledger:
    """Thread-safe tally of model usage, keyed by agent.

    Thread-safe because it has to be: section drafting fans out across threads
    and every branch records into the same ledger.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])

    def record(self, agent: str, model: str, input_tokens: int, output_tokens: int) -> None:
        """Add one call. Called once per LLM round-trip."""
        with self._lock:
            row = self._rows[(agent or "unattributed", model or "unknown")]
            row[0] += 1
            row[1] += int(input_tokens or 0)
            row[2] += int(output_tokens or 0)

    def meter(self, agent: str):
        """A callable the agent layer can use without knowing about Ledger."""
        return lambda model, input_tokens, output_tokens: self.record(
            agent, model, input_tokens, output_tokens
        )

    def by_agent(self) -> dict[str, Spend]:
        """Spend per agent, in the order the agents first spent anything."""
        out: dict[str, Spend] = {}
        with self._lock:
            rows = list(self._rows.items())
        for (agent, model), (calls, inp, out_tok) in rows:
            out[agent] = out.get(agent, Spend()) + _spend(model, calls, inp, out_tok)
        return out

    def total(self) -> Spend:
        total = Spend()
        for spend in self.by_agent().values():
            total = total + spend
        return total

    def unpriced_models(self) -> set[str]:
        """Models seen that PRICES has no entry for."""
        with self._lock:
            models = {model for _, model in self._rows}
        return {m for m in models if price_for(m) is None}

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._rows)


def _spend(model: str, calls: int, input_tokens: int, output_tokens: int) -> Spend:
    rates = price_for(model)
    if rates is None:
        return Spend(calls, input_tokens, output_tokens, 0.0, priced=False)
    per_in, per_out = rates
    cost = (input_tokens * per_in + output_tokens * per_out) / 1_000_000
    return Spend(calls, input_tokens, output_tokens, cost, priced=True)


def format_usd(amount: float) -> str:
    """Report sub-cent amounts honestly instead of rounding them to $0.00."""
    if amount and amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"
