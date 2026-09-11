from conftest import FakeChatModel, make_deps, run_writer_pass

from mas.agents import make_research_node, make_reviewer_node
from mas.agents.writer import plan_sections
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
            issues=[
                Issue(severity="major", section="Competitive Position", problem="thin", fix="add rivals")
            ],
        ),
    )

    out = run_writer_pass(make_deps(model), state)

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

    out = run_writer_pass(make_deps(model), state)
    assert set(out["sections"].values()) == {"REWRITTEN"}


def test_writer_renders_draft_with_headings_in_outline_order(sample_outline):
    model = FakeChatModel(text_handler=lambda messages, m: "body")
    state = initial_state("Company X", "Q4")
    state["outline"] = sample_outline

    draft = run_writer_pass(make_deps(model), state)["draft"]
    assert draft.startswith("# Company X — Q4 Market Research")
    assert draft.index("## Executive Summary") < draft.index("## Competitive Position")


def test_the_first_wave_drafts_body_sections_only(sample_outline):
    """The executive summary waits for something to summarise."""
    state = initial_state("Company X", "Q4")
    state["outline"] = sample_outline

    tasks = plan_sections(state)
    assert [t["heading"] for t in tasks] == ["Competitive Position"]
    assert all(t["issues"] == [] for t in tasks)


def test_the_second_wave_drafts_summaries_with_the_bodies_visible(sample_outline):
    """Siblings are the whole point: without them the summary repeats them."""
    state = initial_state("Company X", "Q4")
    state.update(outline=sample_outline, sections={"Competitive Position": "rivals text"})

    tasks = plan_sections(state)
    assert [t["heading"] for t in tasks] == ["Executive Summary"]
    assert tasks[0]["siblings"] == {"Competitive Position": "rivals text"}


def test_an_outline_of_only_summaries_still_dispatches():
    """A degenerate outline must not stall waiting for bodies that do not exist."""
    from mas.state import Outline, Section

    state = initial_state("Company X", "Q4")
    state["outline"] = Outline(
        title="T",
        sections=[Section(heading="Executive Summary", purpose="p"),
                  Section(heading="Outlook", purpose="p")],
    )
    assert len(plan_sections(state)) == 2


def test_plan_sections_emits_nothing_when_every_section_is_approved(sample_outline):
    """No flagged sections means no work to fan out."""
    state = initial_state("Company X", "Q4")
    state.update(
        outline=sample_outline,
        sections={"Executive Summary": "a", "Competitive Position": "b"},
        review=Review(approved=False, issues=[]),
    )
    assert plan_sections(state) == []


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


# ------------------------------------- the reviewer must not review the footers


def test_the_reviewer_never_sees_the_generated_footers():
    """A live Confluent run had it fact-check the Provenance footer.

    It raised "10 of 10 findings are backed by a retrieved source" as an
    unsupported claim against a section that did not contain it. That objection
    is unanswerable: the footer is regenerated from state on every assemble, so
    no revision the Writer makes can remove it, and it would return every pass.
    """
    from mas.agents.reviewer import make_reviewer_node

    seen = {}

    def capture(schema, messages, model):
        seen["prompt"] = messages[1]["content"]
        return Review(approved=True, issues=[])

    model = FakeChatModel(handlers={"Review": capture})
    draft = (
        "# Acme\n\n## Financials\n\nRevenue was $2.1 billion [0].\n\n"
        "---\n\n## Provenance\n\n"
        "10 of 10 findings are backed by a retrieved source; 0 rest on model "
        "recollection and need verification.\n"
    )

    make_reviewer_node(make_deps(model))({
        "company": "Acme", "quarter": "Q1 2025",
        "findings": [Finding(claim="Revenue was $2.1 billion.", topic="fin", source_ids=[0])],
        "sources": [Source(title="Q1")],
        "draft": draft, "revision": 1, "max_revisions": 2,
    })

    assert "Revenue was $2.1 billion" in seen["prompt"], "the report body must still be reviewed"
    assert "## Provenance" not in seen["prompt"]
    assert "backed by a retrieved source" not in seen["prompt"]


def test_the_review_status_footer_is_stripped_too():
    """A resumed run re-reviews a draft that already carries a verdict."""
    from mas.agents.base import report_body
    from mas.agents.writer import stamp_review

    stamped = stamp_review(
        "# Acme\n\n## Financials\n\nRevenue rose.\n\n---\n\n## Provenance\n\nAll sourced.\n",
        Review(approved=False, issues=[Issue(severity="blocker", problem="p", fix="f")]),
    )
    body = report_body(stamped)

    assert "Revenue rose." in body
    assert "Review status" not in body
    assert "Provenance" not in body
