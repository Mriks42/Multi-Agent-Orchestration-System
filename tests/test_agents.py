from conftest import FakeChatModel, make_deps

from mas.agents import make_research_node, make_reviewer_node, make_writer_node
from mas.state import Finding, Issue, Review, Source, initial_state


def test_research_drops_citations_that_point_past_the_source_list():
    """A hallucinated [7] must never reach the writer as a real footnote."""
    sources = [Source(title="only source", url="https://example.com")]

    def handler(schema, messages, model):
        if schema.__name__ == "_Queries":
            return schema(queries=["q1"])
        return schema(findings=[Finding(claim="c", topic="t", source_ids=[0, 7, -1])])

    deps = make_deps(
        FakeChatModel(handlers={"_Queries": handler, "_Findings": handler}),
        search=lambda q, n=5: sources,
    )
    out = make_research_node(deps)(initial_state("Company X", "Q4"))
    assert out["findings"][0].source_ids == [0]


def test_research_deduplicates_sources_across_queries():
    dupe = Source(title="same", url="https://example.com/a")

    def handler(schema, messages, model):
        if schema.__name__ == "_Queries":
            return schema(queries=["q1", "q2", "q3"])
        return schema(findings=[])

    deps = make_deps(
        FakeChatModel(handlers={"_Queries": handler, "_Findings": handler}),
        search=lambda q, n=5: [dupe],
    )
    out = make_research_node(deps)(initial_state("Company X", "Q4"))
    assert len(out["sources"]) == 1


def test_writer_only_rewrites_the_sections_the_reviewer_flagged(sample_outline):
    """An approved section must survive a revision pass untouched."""
    model = FakeChatModel(text_handler=lambda messages, m: "REWRITTEN")
    state = initial_state("Company X", "Q4")
    state.update(
        outline=sample_outline,
        sections={"Executive Summary": "ORIGINAL", "Competitive Position": "ORIGINAL"},
        revision=1,
        review=Review(
            approved=False,
            issues=[Issue(severity="major", section="Competitive Position", problem="thin", fix="add rivals")],
        ),
    )

    out = make_writer_node(make_deps(model))(state)

    assert out["sections"]["Executive Summary"] == "ORIGINAL"
    assert out["sections"]["Competitive Position"] == "REWRITTEN"
    assert len(model.calls) == 1, "only the flagged section should cost an LLM call"
    assert out["revision"] == 2


def test_writer_applies_report_wide_issues_to_every_section(sample_outline):
    """An issue with no section named belongs to the whole report."""
    model = FakeChatModel(text_handler=lambda messages, m: "REWRITTEN")
    state = initial_state("Company X", "Q4")
    state.update(
        outline=sample_outline,
        sections={"Executive Summary": "ORIGINAL", "Competitive Position": "ORIGINAL"},
        revision=1,
        review=Review(
            approved=False,
            issues=[Issue(severity="major", section="", problem="no citations", fix="cite sources")],
        ),
    )

    out = make_writer_node(make_deps(model))(state)
    assert set(out["sections"].values()) == {"REWRITTEN"}


def test_writer_renders_draft_with_headings_in_outline_order(sample_outline):
    model = FakeChatModel(text_handler=lambda messages, m: "body")
    state = initial_state("Company X", "Q4")
    state["outline"] = sample_outline

    draft = make_writer_node(make_deps(model))(state)["draft"]
    assert draft.startswith("# Company X — Q4 Market Research")
    assert draft.index("## Executive Summary") < draft.index("## Competitive Position")


def test_reviewer_overrides_approval_that_contradicts_its_own_issues():
    """The exit condition depends on `approved`, so it is enforced, not trusted."""
    def handler(schema, messages, model):
        return Review(
            approved=True,
            issues=[Issue(severity="blocker", problem="invented figure", fix="remove")],
        )

    model = FakeChatModel(handlers={"Review": handler})
    state = initial_state("Company X", "Q4")
    state["draft"] = "text"

    assert make_reviewer_node(make_deps(model, reviewer=model))(state)["review"].approved is False


def test_reviewer_leaves_a_clean_approval_alone():
    def handler(schema, messages, model):
        return Review(approved=True, issues=[Issue(severity="minor", problem="wordy", fix="trim")])

    model = FakeChatModel(handlers={"Review": handler})
    state = initial_state("Company X", "Q4")
    state["draft"] = "text"

    assert make_reviewer_node(make_deps(model, reviewer=model))(state)["review"].approved is True
