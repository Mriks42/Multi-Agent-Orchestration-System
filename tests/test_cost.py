"""What a run cost, and the ways that number could lie.

The point of this file is less the arithmetic than the honesty of it: an
unpriced model must not report $0.00, a sub-cent run must not round to nothing,
and a figure that only counts some of the calls must say so.
"""

from __future__ import annotations

import threading

import pytest
from conftest import FakeChatModel, make_deps

from mas.cost import PRICES, Ledger, Spend, format_usd, price_for, usd_places
from mas.graph import build_graph
from mas.state import Finding, Outline, Review, Section, Source


# ----------------------------------------------------------------- the prices


def test_a_dated_snapshot_prices_as_its_base_model():
    """"gpt-4o-2024-11-20" is not listed separately and must not go unpriced."""
    assert price_for("gpt-4o-2024-11-20") == PRICES["gpt-4o"]


def test_mini_is_not_charged_at_the_full_model_rate():
    """The prefix match must prefer the longest name.

    "gpt-4o-mini-2024-07-18" starts with "gpt-4o" too, and charging it at
    gpt-4o's rate would overstate a drafting-heavy run by roughly 16x.
    """
    assert price_for("gpt-4o-mini-2024-07-18") == PRICES["gpt-4o-mini"]
    assert price_for("gpt-4o-mini") != price_for("gpt-4o")


def test_an_unknown_model_has_no_price_rather_than_a_free_one():
    assert price_for("some-model-released-next-year") is None


# ----------------------------------------------------------------- the ledger


def test_cost_follows_the_published_rates():
    ledger = Ledger()
    ledger.record("Writer Agent", "gpt-4o-mini", 1_000_000, 1_000_000)

    spend = ledger.by_agent()["Writer Agent"]
    assert spend.cost_usd == pytest.approx(0.15 + 0.60)
    assert spend.tokens == 2_000_000
    assert spend.priced


def test_an_unpriced_model_reports_tokens_but_not_a_cost_of_zero():
    """A measure that moves the wrong way is worse than one that does not move.

    Reporting $0.00 for a model the table has never heard of would make an
    expensive run look free, which is the failure `mas-ablate` exists to avoid.
    """
    ledger = Ledger()
    ledger.record("Writer Agent", "brand-new-model", 1000, 500)

    spend = ledger.total()
    assert spend.tokens == 1500, "tokens are still counted"
    assert not spend.priced, "the total must admit it is incomplete"
    assert ledger.unpriced_models() == {"brand-new-model"}


def test_one_unpriced_call_taints_the_whole_total():
    """Mixing a known and an unknown model must not produce a confident total."""
    ledger = Ledger()
    ledger.record("Writer Agent", "gpt-4o-mini", 1000, 500)
    ledger.record("Reviewer Agent", "mystery-model", 1000, 500)

    assert ledger.by_agent()["Writer Agent"].priced
    assert not ledger.total().priced


def test_spend_is_tallied_per_agent():
    ledger = Ledger()
    ledger.record("Writer Agent", "gpt-4o-mini", 100, 100)
    ledger.record("Writer Agent", "gpt-4o-mini", 100, 100)
    ledger.record("Reviewer Agent", "gpt-4o", 100, 100)

    by_agent = ledger.by_agent()
    assert by_agent["Writer Agent"].calls == 2
    assert by_agent["Reviewer Agent"].calls == 1
    assert ledger.total().calls == 3
    assert by_agent["Reviewer Agent"].cost_usd > by_agent["Writer Agent"].cost_usd


def test_an_agent_using_two_models_sums_both():
    ledger = Ledger()
    ledger.record("Writer Agent", "gpt-4o-mini", 1_000_000, 0)
    ledger.record("Writer Agent", "gpt-4o", 1_000_000, 0)

    assert ledger.by_agent()["Writer Agent"].cost_usd == pytest.approx(0.15 + 2.50)


def test_the_ledger_survives_concurrent_writers():
    """Section drafting fans out across threads into one ledger."""
    ledger = Ledger()

    def spend():
        for _ in range(200):
            ledger.record("Writer Agent", "gpt-4o-mini", 10, 10)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ledger.total().calls == 1600, "a record was lost to a race"
    assert ledger.total().input_tokens == 16_000


def test_an_empty_ledger_is_falsey():
    assert not Ledger()
    ledger = Ledger()
    ledger.record("Writer Agent", "gpt-4o-mini", 1, 1)
    assert ledger


def test_spend_adds_without_mutating():
    a = Spend(calls=1, input_tokens=10, output_tokens=5, cost_usd=1.0)
    b = Spend(calls=2, input_tokens=20, output_tokens=5, cost_usd=2.0)
    assert (a + b).calls == 3
    assert a.calls == 1, "Spend is frozen; adding must not mutate"


# ---------------------------------------------------------------- the display


def test_a_sub_cent_run_does_not_round_away_to_nothing():
    """A default run costs well under a cent; "$0.00" would read as free."""
    assert format_usd(0.0031) == "$0.0031"
    assert format_usd(1.5) == "$1.50"
    assert format_usd(0.0) == "$0.00"


def test_one_precision_per_column_keeps_the_rows_within_a_last_place_of_the_total():
    """Uniform precision shrinks the mismatch; it cannot remove it.

    Rounding each row and separately rounding their exact sum will not always
    agree: a live Cloudflare run displayed rows summing to $0.0318 against a
    total of $0.0317. Every figure was its own honest rounding.

    The fix that was actually needed was uniform precision. Mixed precision --
    "$0.03" beside "$0.0045" -- made a $0.0045 row look like it belonged to a
    $0.03 column, an apparent $0.0345 against a total of $0.03. That is an 18%
    discrepancy; this is one unit in the last displayed place.

    Forcing an exact sum would mean printing some row as other than its own
    rounding, which trades a visible rounding residual for an invisible wrong
    number. Not worth it. This pins the bound instead.
    """
    cases = [
        [0.0259725, 0.0047124, 0.0005586, 0.0004671],   # the Cloudflare run
        [0.0317, 0.0051, 0.0005, 0.0005],               # the Shopify run
        [7.0, 0.69, 0.004],                             # dollar scale
    ]
    for rows in cases:
        total = sum(rows)
        places = usd_places(rows + [total])
        shown = [float(format_usd(r, places).lstrip("$")) for r in rows]
        shown_total = float(format_usd(total, places).lstrip("$"))
        # Each row can move by at most half a unit in the last place.
        bound = len(rows) * 0.5 * 10 ** -places + 1e-12
        assert abs(sum(shown) - shown_total) <= bound, (rows, sum(shown), shown_total)


def test_a_dollar_scale_run_is_not_shown_to_four_decimals():
    """Precision follows the column: cents need four places, dollars do not."""
    assert usd_places([7.0, 0.69]) == 2
    assert usd_places([0.026, 0.0045]) == 4
    assert format_usd(7.0, usd_places([7.0, 0.69])) == "$7.00"


def test_an_empty_column_does_not_explode():
    assert usd_places([]) == 4


# ------------------------------------------------------------------ end to end


def test_a_whole_run_attributes_its_spend_to_the_four_agents():
    """The ledger must fill in from a real graph run, not just direct calls."""
    def handler(schema, messages, model):
        name = schema.__name__
        if name == "_Queries":
            return schema(queries=["q1"])
        if name == "_Findings":
            return schema(findings=[Finding(claim="Revenue was $2.1B", topic="fin",
                                            source_ids=[0])])
        if name == "Outline":
            return Outline(title="Acme — Q1 2025", sections=[
                Section(heading="Financials", purpose="numbers"),
            ])
        raise AssertionError(name)

    writer = FakeChatModel(handlers={k: handler for k in ("_Queries", "_Findings", "Outline")},
                           text_handler=lambda m, mo: "body [0]", usage=(100, 50))
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: Review(approved=True)},
                             usage=(200, 20), model_name="gpt-4o")
    source = Source(title="Q1", url="https://example.com", snippet="revenue")
    deps = make_deps(writer, reviewer, search=lambda q, n=5: [source])

    build_graph(deps).invoke({
        "company": "Acme", "quarter": "Q1 2025", "max_revisions": 1,
    })

    by_agent = deps.ledger.by_agent()
    assert set(by_agent) >= {"Research Agent", "Planning Agent", "Writer Agent",
                             "Reviewer Agent"}, by_agent
    assert by_agent["Research Agent"].calls == 2, "queries, then findings"
    assert by_agent["Planning Agent"].calls == 1
    assert deps.ledger.total().cost_usd > 0
    assert deps.ledger.total().priced

    # The Reviewer runs the expensive model, so its per-call cost is higher
    # even though it makes far fewer calls than the Writer.
    assert by_agent["Reviewer Agent"].input_tokens == 200
