"""The eval metrics must be deterministic, or they cannot detect a regression."""

from __future__ import annotations

from conftest import FakeChatModel, make_deps

from mas.evals.cases import CASES, by_name
from mas.evals.metrics import (
    Metrics,
    aggregate,
    cited_section_rate,
    orphan_citations,
    score_run,
    sourced_finding_rate,
    unattributed_figures,
)
from mas.evals.runner import compare
from mas.evals.seeded import PROBES, ProbeResult, run_probes, score_probes
from mas.state import Finding, Issue, Review, Source, initial_state

SOURCED = Finding(claim="c", topic="t", source_ids=[0])
UNSOURCED = Finding(claim="c", topic="t")


# ------------------------------------------------------------------- metrics


def test_sourced_finding_rate_counts_backed_findings():
    assert sourced_finding_rate([SOURCED, UNSOURCED]) == 0.5
    assert sourced_finding_rate([SOURCED]) == 1.0
    assert sourced_finding_rate([]) == 0.0, "no findings must not divide by zero"


def test_orphan_citations_counts_only_out_of_range_indices():
    sources = [Source(title="a"), Source(title="b")]
    assert orphan_citations("text [0] and [1]", sources) == 0
    assert orphan_citations("text [2] and [47]", sources) == 2


def test_unattributed_figures_flags_bare_claims_but_not_hedged_ones():
    assert unattributed_figures("Revenue was $3 billion.") == 1
    assert unattributed_figures("Revenue was reportedly $3 billion.") == 0
    assert unattributed_figures("Revenue was estimated at $3 billion.") == 0
    assert unattributed_figures("The team shipped a product.") == 0


def test_unattributed_figures_ignores_the_generated_provenance_footer():
    """The footer states counts as fact by design; counting it would be noise."""
    draft = (
        "# R\n\n## S\n\nAll good.\n\n---\n\n## Provenance\n\n"
        "9 of 10 findings are backed by a retrieved source; 1 rests on recollection."
    )
    assert unattributed_figures(draft) == 0


def test_cited_section_rate_measures_sections_carrying_evidence():
    draft = "# R\n\n## A\n\nClaim [0].\n\n## B\n\nNo evidence here.\n"
    assert cited_section_rate(draft) == 0.5
    assert cited_section_rate("# R\n\nno sections") == 0.0


def test_score_run_reduces_a_finished_state():
    state = initial_state("Acme", "Q4")
    state.update(
        findings=[SOURCED, UNSOURCED],
        sources=[Source(title="a")],
        draft="# R\n\n## A\n\nRevenue was $3 billion [0].\n\n---\n\n## Provenance\n\n1 of 2.",
        review=Review(approved=True),
        revision=2,
    )

    m = score_run(state, duration_s=12.34)
    assert m.company == "Acme"
    assert m.sourced_finding_rate == 0.5
    assert m.citation_count == 1
    assert m.orphan_citation_count == 0
    assert m.unattributed_figure_count == 1
    assert m.provenance_footer is True
    assert m.approved is True
    assert m.revisions_used == 2
    assert m.section_count == 1
    assert m.duration_s == 12.3


def test_score_run_counts_open_issues_only_when_unapproved():
    state = initial_state("Acme", "Q4")
    state["review"] = Review(
        approved=False, issues=[Issue(severity="blocker", problem="p", fix="f")]
    )
    assert score_run(state).open_issue_count == 1

    state["review"] = Review(approved=True, issues=[Issue(severity="minor", problem="p", fix="f")])
    assert score_run(state).open_issue_count == 0


def test_score_run_survives_an_empty_state():
    """A crashed run must still produce a scoreable row."""
    m = score_run(initial_state("Acme", "Q4"))
    assert m.word_count == 0 and m.provenance_footer is False


# ----------------------------------------------------------------- aggregate


def test_aggregate_excludes_failures_from_averages_but_reports_them():
    runs = [
        Metrics(company="A", sourced_finding_rate=1.0, citation_count=10),
        Metrics(company="B", sourced_finding_rate=0.0, citation_count=0),
        Metrics(company="C", error="Timeout"),
    ]
    summary = aggregate(runs)

    assert summary == {**summary, "runs": 3, "succeeded": 2, "failed": 1}
    assert summary["sourced_finding_rate"] == 0.5, "the failed run must not drag the mean"
    assert summary["failures"] == [{"company": "C", "error": "Timeout"}]


def test_aggregate_of_all_failures_does_not_divide_by_zero():
    assert aggregate([Metrics(company="A", error="boom")])["sourced_finding_rate"] == 0.0


# ------------------------------------------------------------------ compare


def test_compare_knows_which_direction_is_an_improvement():
    """More citations is better; more unattributed figures is worse."""
    baseline = {"summary": {"citation_count": 10, "unattributed_figure_count": 2}}
    current = {"summary": {"citation_count": 15, "unattributed_figure_count": 5}}

    rows = {r["metric"]: r for r in compare(current, baseline)}
    assert rows["citation_count"]["improved"] is True
    assert rows["unattributed_figure_count"]["improved"] is False


def test_compare_without_a_baseline_returns_nothing():
    assert compare({"summary": {}}, None) == []


# ------------------------------------------------------------------- probes


def test_probe_set_has_planted_defects_and_a_clean_control():
    """Without a control, a reviewer that flags everything would score 100%."""
    assert any(p.should_flag for p in PROBES)
    assert any(not p.should_flag for p in PROBES)
    assert sum(1 for p in PROBES if not p.should_flag) >= 1


def test_probes_run_against_a_stub_reviewer():
    """A reviewer that objects to everything: full recall, but a false positive."""
    def always_flag(schema, messages, model):
        return Review(approved=False, issues=[Issue(severity="blocker", problem="p", fix="f")])

    model = FakeChatModel(handlers={"Review": always_flag})
    results = run_probes(make_deps(model, reviewer=model))
    summary = score_probes(results)

    assert summary["catch_rate"] == 1.0
    assert summary["false_positives"] == 1, "the clean control must expose over-flagging"


def test_a_permissive_model_still_cannot_pass_an_orphan_citation():
    """The mechanical citation check does not depend on the model's judgement.

    With a reviewer that approves everything, the only probe still caught is the
    orphan citation — which is the point of checking it in code.
    """
    def never_flag(schema, messages, model):
        return Review(approved=True, issues=[])

    model = FakeChatModel(handlers={"Review": never_flag})
    summary = score_probes(run_probes(make_deps(model, reviewer=model)))

    assert summary["false_positives"] == 0
    assert summary["missed"] == ["fabricated_figure", "contradicted_finding",
                                 "unsourced_stated_as_fact"]
    assert "orphan_citation" not in summary["missed"]


def test_probe_errors_are_recorded_not_raised():
    def explode(schema, messages, model):
        raise RuntimeError("api down")

    model = FakeChatModel(handlers={"Review": explode})
    summary = score_probes(run_probes(make_deps(model, reviewer=model)))
    assert len(summary["errors"]) == len(PROBES)


def test_probe_result_correctness_compares_expectation_to_outcome():
    assert ProbeResult("x", should_flag=True, flagged=True, what="").correct
    assert not ProbeResult("x", should_flag=True, flagged=False, what="").correct
    assert ProbeResult("x", should_flag=False, flagged=False, what="").correct


# -------------------------------------------------------------------- cases


def test_case_set_spans_high_and_low_evidence_coverage():
    """A suite of only mega-caps would hide the fabrication behaviour."""
    coverages = {c.coverage for c in CASES}
    assert {"high", "low"} <= coverages


def test_by_name_selects_cases_and_rejects_unknown_ones():
    assert [c.company for c in by_name(["nvidia"])] == ["NVIDIA"]
    try:
        by_name(["NotARealCompany"])
    except ValueError as exc:
        assert "notarealcompany" in str(exc).lower()
    else:
        raise AssertionError("unknown case should raise")


# ----------------------------------------------- mechanical citation check


def test_check_citations_flags_only_out_of_range_indices():
    from mas.agents.reviewer import check_citations

    sources = [Source(title="a"), Source(title="b")]
    assert check_citations("cites [0] and [1]", sources) == []

    issues = check_citations("cites [0] and [47] and [47]", sources)
    assert len(issues) == 1, "the same orphan must not be reported twice"
    assert issues[0].severity == "blocker"
    assert "[47]" in issues[0].problem


def test_check_citations_with_no_sources_tells_the_writer_to_remove_them():
    from mas.agents.reviewer import check_citations

    issues = check_citations("cites [0]", [])
    assert len(issues) == 1
    assert "no sources were retrieved" in issues[0].fix


# ------------------------------------------------- comparability of baselines


def _run(label, companies, model="gpt-4o-mini"):
    return {"label": label, "model": model,
            "cases": [{"company": c} for c in companies], "summary": {}}


def test_runs_over_the_same_cases_are_comparable():
    a = _run("r1", ["NVIDIA", "Stripe"])
    b = _run("r2", ["Stripe", "NVIDIA"])  # order must not matter
    from mas.evals.runner import comparable

    assert comparable(a, b) == ""


def test_a_smoke_run_is_not_a_baseline_for_a_full_run():
    """Diffing 2 cases against 12 measures the case list, not the pipeline."""
    from mas.evals.runner import comparable

    smoke = _run("smoke", ["NVIDIA", "Stripe"])
    full = _run("full", [f"C{i}" for i in range(12)])

    reason = comparable(full, smoke)
    assert "2 case(s)" in reason and "12" in reason


def test_a_model_change_is_flagged_as_confounding():
    from mas.evals.runner import comparable

    old = _run("r1", ["NVIDIA"], model="gpt-4o-mini")
    new = _run("r2", ["NVIDIA"], model="gpt-4o")

    assert "model change" in comparable(new, old)


def test_no_baseline_is_not_an_incomparability():
    from mas.evals.runner import comparable

    assert comparable(_run("r1", ["NVIDIA"]), None) == ""


def test_load_baseline_prefers_a_run_over_the_same_cases(tmp_path):
    """Otherwise the newest run wins even when it covers different companies."""
    import json as _json

    from mas.evals.cases import Case
    from mas.evals.runner import load_baseline

    (tmp_path / "eval-001.json").write_text(
        _json.dumps(_run("001", ["NVIDIA", "Stripe"])), encoding="utf-8")
    (tmp_path / "eval-002.json").write_text(
        _json.dumps(_run("002", ["Microsoft"])), encoding="utf-8")

    smoke_cases = [Case("NVIDIA"), Case("Stripe")]
    assert load_baseline(tmp_path, like=smoke_cases)["label"] == "001"


def test_load_baseline_falls_back_to_the_newest_when_nothing_matches(tmp_path):
    import json as _json

    from mas.evals.cases import Case
    from mas.evals.runner import load_baseline

    (tmp_path / "eval-001.json").write_text(
        _json.dumps(_run("001", ["NVIDIA"])), encoding="utf-8")

    assert load_baseline(tmp_path, like=[Case("Braze")])["label"] == "001"
