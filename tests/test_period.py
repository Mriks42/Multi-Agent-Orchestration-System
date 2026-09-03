"""Period validation and the period-discipline prompt block."""

from __future__ import annotations

import pytest
from conftest import FakeChatModel, make_deps, run_writer_pass

from mas.period import InvalidPeriod, discipline, fiscal_hint, validate
from mas.state import Issue, Review, initial_state


@pytest.mark.parametrize(
    "period", ["Q1 2025", "Q4 2025", "FY2024", "H1 2025", "2025", "2023-2025", "full year 2024"]
)
def test_any_period_naming_a_year_is_accepted(period):
    """The field stays free-form; only the missing year is rejected."""
    assert validate(period) == period


@pytest.mark.parametrize("period", ["Q4", "Q1", "last quarter", "H1", ""])
def test_a_period_without_a_year_is_rejected(period):
    """Without a year the model picks one silently and never says which."""
    with pytest.raises(InvalidPeriod):
        validate(period)


def test_the_error_suggests_a_concrete_fix():
    with pytest.raises(InvalidPeriod, match="Q4 2025"):
        validate("Q4")


def test_validate_strips_surrounding_whitespace():
    assert validate("  Q1 2025  ") == "Q1 2025"


# ------------------------------------------------------------- fiscal hints


def test_companies_with_offset_fiscal_years_get_a_warning():
    hint = fiscal_hint("NVIDIA")
    assert "fiscal" in hint.lower()
    assert "January" in hint


def test_fiscal_hint_lookup_is_case_insensitive():
    """The lookup ignores case; the text echoes the caller's spelling."""
    assert "fiscal year is offset" in fiscal_hint("nvidia")
    assert "fiscal year is offset" in fiscal_hint("NVIDIA")


def test_companies_without_a_known_offset_get_no_hint():
    """Silence rather than a guess: the list is a hint, not a database."""
    assert fiscal_hint("Shopify") == ""
    assert fiscal_hint("Some Startup") == ""


# --------------------------------------------------------- discipline block


def test_discipline_names_the_requested_period():
    text = discipline("Shopify", "Q1 2025")
    assert "Q1 2025" in text
    assert "trailing-twelve-month" in text


def test_discipline_tells_the_model_to_suspect_a_period_mismatch():
    """A live Shopify run blamed 'data interpretation' for what was almost
    certainly four different periods."""
    text = discipline("Shopify", "Q1 2025")
    assert "period mismatch" in text
    assert "Never average, merge or silently pick" in text


def test_discipline_appends_the_fiscal_warning_when_one_applies():
    assert "fiscal year is offset" in discipline("NVIDIA", "Q4 2025")
    assert "fiscal year is offset" not in discipline("Shopify", "Q1 2025")


def test_discipline_reaches_the_writer_prompt(sample_outline):
    """The rule is useless if it never gets into the prompt."""
    seen = []
    model = FakeChatModel(text_handler=lambda messages, m: seen.append(messages) or "body")
    state = initial_state("NVIDIA", "Q4 2025")
    state["outline"] = sample_outline

    run_writer_pass(make_deps(model), state)
    prompt = seen[0][1]["content"]

    assert "PERIOD DISCIPLINE" in prompt
    assert "fiscal year is offset" in prompt


# ------------------------------------------------------------ trace accuracy


def test_a_revision_pass_reports_what_it_revised_not_what_exists(sample_outline):
    """The fan-out refactor made every pass announce the total section count."""
    model = FakeChatModel(text_handler=lambda messages, m: "REWRITTEN")
    state = initial_state("Acme", "Q4 2025")
    state.update(
        outline=sample_outline,
        sections={"Executive Summary": "ORIGINAL", "Competitive Position": "ORIGINAL"},
        revision=1,
        review=Review(
            approved=False,
            issues=[Issue(severity="major", section="Competitive Position",
                          problem="thin", fix="add rivals")],
        ),
    )

    trace = run_writer_pass(make_deps(model), state)["trace"][-1]
    assert "1 of 2 section(s) revised" in trace, trace


def test_a_first_pass_reports_sections_drafted(sample_outline):
    model = FakeChatModel(text_handler=lambda messages, m: "body")
    state = initial_state("Acme", "Q4 2025")
    state["outline"] = sample_outline

    trace = run_writer_pass(make_deps(model), state)["trace"][-1]
    assert "2 section(s) drafted" in trace, trace
