"""The judge's bias controls must be provable, or its scores mean nothing."""

from __future__ import annotations

from conftest import FakeChatModel, make_deps

from mas.evals.judge import (
    CRITERIA,
    Agreement,
    Comparison,
    CriterionScore,
    JudgeScore,
    Verdict,
    agreement,
    compare_reports,
    score_report,
)
from mas.state import initial_state

DRAFT_A = "# A\n\n## S\n\nRevenue was $2.1 billion [0].\n\n---\n\n## Provenance\n\n1 of 1."
DRAFT_B = "# B\n\n## S\n\nThings went well generally.\n"


# ------------------------------------------------------- position-bias control


def test_a_consistent_winner_across_both_orderings_is_a_real_win():
    c = Comparison(company="X", forward="A", swapped="B")
    assert c.winner == "A", "A first says A; B first says B — the same report won twice"
    assert c.order_dependent is False


def test_a_verdict_that_flips_with_order_is_recorded_as_a_tie():
    """The judge picking whichever came first is position bias, not a result."""
    c = Comparison(company="X", forward="A", swapped="A")
    assert c.order_dependent is True
    assert c.winner == "tie"


def test_b_can_win_consistently_too():
    c = Comparison(company="X", forward="B", swapped="A")
    assert c.winner == "B"
    assert c.order_dependent is False


def test_a_genuine_tie_in_both_orderings_stays_a_tie():
    c = Comparison(company="X", forward="tie", swapped="tie")
    assert c.winner == "tie"
    assert c.order_dependent is False


def test_a_failed_comparison_reports_error_not_a_winner():
    c = Comparison(company="X", error="RuntimeError: api down")
    assert c.winner == "error"
    assert c.order_dependent is False


def test_compare_runs_both_orderings_and_swaps_the_reports():
    """Two calls, with the drafts in opposite positions."""
    seen = []

    def handler(schema, messages, model):
        seen.append(messages[1]["content"])
        return Verdict(winner="A", reason="first one looked better")

    model = FakeChatModel(handlers={"Verdict": handler})
    deps = make_deps(model)
    deps.judge_llm = model

    result = compare_reports(deps, "X", "Q4", DRAFT_A, DRAFT_B)

    assert len(seen) == 2, "a single ordering measures position bias, not quality"
    assert seen[0].index("Revenue was") < seen[0].index("Things went well")
    assert seen[1].index("Things went well") < seen[1].index("Revenue was")
    # Said "A" both times — i.e. whichever was first — so it must not count as a win.
    assert result.winner == "tie"
    assert result.order_dependent is True


def test_compare_records_an_exception_instead_of_raising():
    def explode(schema, messages, model):
        raise RuntimeError("api down")

    model = FakeChatModel(handlers={"Verdict": explode})
    deps = make_deps(model)
    deps.judge_llm = model

    result = compare_reports(deps, "X", "Q4", DRAFT_A, DRAFT_B)
    assert result.winner == "error"
    assert "api down" in result.error


# --------------------------------------------------------------------- scoring


def test_score_report_strips_the_generated_provenance_footer():
    """The footer is generated, not written — judging it would score our own code."""
    seen = []

    def handler(schema, messages, model):
        seen.append(messages[1]["content"])
        return JudgeScore(
            scores=[CriterionScore(criterion=c, score=4, reason="r") for c in CRITERIA],
            overall=4,
        )

    model = FakeChatModel(handlers={"JudgeScore": handler})
    deps = make_deps(model)
    deps.judge_llm = model

    result = score_report(deps, {**initial_state("X", "Q4"), "draft": DRAFT_A})

    assert "## Provenance" not in seen[0]
    assert "Revenue was" in seen[0]
    assert result.mean == 4.0
    assert set(result.by_criterion()) == set(CRITERIA)


def test_rubric_tells_the_judge_that_length_is_not_quality():
    """Verbosity bias is the judge's most common failure; it is stated explicitly."""
    seen = []
    model = FakeChatModel(handlers={"JudgeScore": lambda s, m, mo: seen.append(m[1]["content"]) or
                                    JudgeScore(scores=[], overall=3)})
    deps = make_deps(model)
    deps.judge_llm = model
    score_report(deps, {**initial_state("X", "Q4"), "draft": DRAFT_A})

    assert "Length is NOT quality" in seen[0]
    assert "Do not reward volume" in seen[0]


def test_judge_score_mean_handles_an_empty_score_list():
    assert JudgeScore(scores=[], overall=3).mean == 0.0


# ------------------------------------------------------------ judge validation


def test_agreement_scores_the_judge_against_human_labels():
    judge = {"a": "A", "b": "B", "c": "tie"}
    human = {"a": "A", "b": "A", "c": "tie"}

    result = agreement(judge, human)
    assert result.n == 3
    assert result.matches == 2
    assert result.rate == 0.667
    assert result.disagreements == [{"item": "b", "judge": "B", "human": "A"}]


def test_agreement_ignores_items_no_human_labelled():
    result = agreement({"a": "A", "unlabelled": "B"}, {"a": "A"})
    assert result.n == 1, "only human-labelled items can be scored"


def test_agreement_refuses_to_pronounce_on_too_few_labels():
    """A high rate over 3 items is noise, and saying so is the point."""
    assert agreement({"a": "A"}, {"a": "A"}).verdict == "insufficient labels to say"


def test_agreement_verdict_thresholds():
    def built(matches, n):
        a = Agreement(n=n, matches=matches)
        return a.verdict

    assert built(18, 20) == "usable"
    assert built(14, 20).startswith("weak")
    assert built(8, 20).startswith("unusable")


def test_agreement_with_no_labels_does_not_divide_by_zero():
    assert Agreement().rate == 0.0


# ------------------------------------------------------------ human labelling


def test_pairs_are_built_per_company_across_runs():
    from mas.evals.label import pairs_from_runs

    runs = [
        {"label": "run1", "drafts": {"NVIDIA Q4 2025": "draft one", "Shopify Q1 2025": "s1"}},
        {"label": "run2", "drafts": {"NVIDIA Q4 2025": "draft two", "Shopify Q1 2025": "s2"}},
    ]
    pairs = pairs_from_runs(runs)

    assert len(pairs) == 2, "one pair per company"
    assert {p.company for p in pairs} == {"NVIDIA", "Shopify"}
    assert all({p.a, p.b} for p in pairs)


def test_pair_order_is_shuffled_so_the_newer_run_is_not_always_first():
    """If the human always sees the new version as A, the label is not blind."""
    from mas.evals.label import pairs_from_runs

    runs = [
        {"label": "old", "drafts": {f"C{i} Q1 2025": "OLD" for i in range(30)}},
        {"label": "new", "drafts": {f"C{i} Q1 2025": "NEW" for i in range(30)}},
    ]
    firsts = [p.a for p in pairs_from_runs(runs, seed=7)]
    assert "OLD" in firsts and "NEW" in firsts, "both orderings should appear"


def test_pairs_skip_companies_with_an_empty_draft():
    from mas.evals.label import pairs_from_runs

    runs = [
        {"label": "run1", "drafts": {"A Q1 2025": "", "B Q1 2025": "text"}},
        {"label": "run2", "drafts": {"A Q1 2025": "text", "B Q1 2025": "text"}},
    ]
    assert [p.company for p in pairs_from_runs(runs)] == ["B"]


def test_labels_round_trip_through_disk(tmp_path):
    from mas.evals.label import LabelSet, Pair

    labels = LabelSet(pairs=[
        Pair(item="x", company="A", quarter="Q1 2025", a="1", b="2", human="A"),
        Pair(item="y", company="B", quarter="Q1 2025", a="1", b="2"),
    ])
    path = labels.save(tmp_path / "labels.json")

    reloaded = LabelSet.load(path)
    assert len(reloaded.pairs) == 2
    assert len(reloaded.labelled) == 1, "an unlabelled pair stays unlabelled"
    assert reloaded.human_labels() == {"x": "A"}


def test_loading_a_missing_label_file_gives_an_empty_set(tmp_path):
    from mas.evals.label import LabelSet

    assert LabelSet.load(tmp_path / "nope.json").pairs == []


def test_score_against_judge_only_replays_labelled_pairs():
    from mas.evals.label import LabelSet, Pair, score_against_judge

    seen = []

    def handler(schema, messages, model):
        seen.append(1)
        return Verdict(winner="A", reason="r")

    model = FakeChatModel(handlers={"Verdict": handler})
    deps = make_deps(model)
    deps.judge_llm = model

    labels = LabelSet(pairs=[
        Pair(item="x", company="A", quarter="Q1 2025", a="1", b="2", human="A"),
        Pair(item="unlabelled", company="B", quarter="Q1 2025", a="1", b="2"),
    ])
    result, judge_labels = score_against_judge(deps, labels)

    assert len(seen) == 2, "one labelled pair, judged in both orderings"
    assert set(judge_labels) == {"x"}
    assert result.n == 1


def test_pair_ids_do_not_reveal_which_run_came_first():
    """labels.json is readable; an id printing run order defeats the shuffle."""
    from mas.evals.label import pairs_from_runs

    runs = [
        {"label": "NVIDIA Q4 2025", "drafts": {"NVIDIA Q4 2025": "old"}},
        {"label": "20260903T030239Z", "drafts": {"NVIDIA Q4 2025": "new"}},
    ]
    runs[0]["label"] = "20260903T022933Z"
    item = pairs_from_runs(runs)[0].item

    assert "20260903T022933Z" not in item
    assert "20260903T030239Z" not in item
    assert item.startswith("NVIDIA Q4 2025|")


def test_pair_ids_are_stable_whichever_way_the_shuffle_lands():
    """A re-build must keep existing labels attached to their pair."""
    from mas.evals.label import pairs_from_runs

    runs = [
        {"label": "r1", "drafts": {"NVIDIA Q4 2025": "one"}},
        {"label": "r2", "drafts": {"NVIDIA Q4 2025": "two"}},
    ]
    ids = {pairs_from_runs(runs, seed=s)[0].item for s in range(6)}
    assert len(ids) == 1, "the id must not depend on the shuffle"
