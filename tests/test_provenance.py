"""Provenance handling: an unsourced finding must never read as verified fact."""

from conftest import FakeChatModel, make_deps, run_writer_pass

from mas.agents.base import format_findings, provenance
from mas.state import Finding, initial_state

SOURCED = Finding(claim="Revenue grew 12%.", topic="financials", source_ids=[0])
UNSOURCED = Finding(claim="Valuation reached $95B.", topic="financials", confidence="low")


def test_unsourced_findings_are_marked_loudly_for_the_model():
    rendered = format_findings([SOURCED, UNSOURCED])
    assert "sources=[0]" in rendered
    assert "UNSOURCED" in rendered


def test_provenance_counts_sourced_against_total():
    assert provenance([SOURCED, UNSOURCED, UNSOURCED]) == (1, 3)
    assert provenance([]) == (0, 0)


def _draft(findings, outline):
    state = initial_state("Company X", "Q4")
    state.update(outline=outline, findings=findings)
    model = FakeChatModel(text_handler=lambda messages, m: "body")
    return run_writer_pass(make_deps(model), state)["draft"]


def test_fully_unsourced_report_is_labelled_unverified(sample_outline):
    draft = _draft([UNSOURCED, UNSOURCED], sample_outline)
    assert "## Provenance" in draft
    assert "None of the 2 findings" in draft
    assert "model recollection" in draft


def test_partly_sourced_report_reports_the_split(sample_outline):
    draft = _draft([SOURCED, UNSOURCED, UNSOURCED], sample_outline)
    assert "1 of 3 findings are backed by a retrieved source" in draft
    assert "2 rest on model recollection and need verification" in draft


def test_single_unsourced_finding_reads_as_singular(sample_outline):
    """The footer appears in every report; "1 rest on" reads as a bug to a reader."""
    draft = _draft([SOURCED, SOURCED, UNSOURCED], sample_outline)
    assert "1 rests on model recollection and needs verification" in draft


def test_report_with_no_findings_says_so(sample_outline):
    assert "No research findings backed this report." in _draft([], sample_outline)


def test_writer_prompt_carries_the_unsourced_rule_to_the_model(sample_outline):
    """The rule is useless if it never reaches the prompt."""
    seen = []
    model = FakeChatModel(text_handler=lambda messages, m: seen.append(messages) or "body")
    state = initial_state("Company X", "Q4")
    state.update(outline=sample_outline, findings=[UNSOURCED])
    run_writer_pass(make_deps(model), state)

    prompt = seen[0][1]["content"]
    assert "UNSOURCED" in prompt
    assert "Never state its figures as established" in prompt
