"""End-to-end graph tests: all four agents, no network."""

from conftest import FakeChatModel, make_deps

from mas.graph import dispatch_sections, run_report
from mas.state import Finding, Issue, Outline, Review, Section, Source, initial_state


def build_models(review_script: list[Review]):
    """Writer/planner model plus a reviewer that returns each verdict in turn."""
    reviews = iter(review_script)

    def writer_handler(schema, messages, model):
        name = schema.__name__
        if name == "_Queries":
            return schema(queries=["q1", "q2"])
        if name == "_Findings":
            return schema(findings=[Finding(claim="Revenue grew 12%.", topic="financials", source_ids=[0])])
        if name == "Outline":
            return Outline(
                title="Company X — Q4 Market Research",
                sections=[
                    Section(heading="Executive Summary", purpose="frame"),
                    Section(heading="Outlook", purpose="predict"),
                ],
            )
        raise AssertionError(f"unexpected schema {name}")

    writer = FakeChatModel(
        handlers={"_Queries": writer_handler, "_Findings": writer_handler, "Outline": writer_handler},
        text_handler=lambda messages, m: f"section body {len(m.calls)}",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda schema, messages, model: next(reviews)})
    return writer, reviewer


def run(review_script, max_revisions=2, search=None):
    writer, reviewer = build_models(review_script)
    deps = make_deps(writer, reviewer, search=search)
    events = []
    state = run_report(
        deps, "Company X", "Q4", max_revisions=max_revisions,
        on_event=lambda node, update: events.append(node),
    )
    return state, events, writer, reviewer


def test_happy_path_runs_each_agent_once_and_produces_a_report():
    state, events, _, reviewer = run([Review(approved=True, summary="good")])

    assert events == ["research", "planning", "write_section", "write_section", "assemble", "reviewer"]
    assert state["review"].approved is True
    assert state["revision"] == 1
    assert "# Company X — Q4 Market Research" in state["draft"]
    assert "## Outlook" in state["draft"]
    assert len(reviewer.calls) == 1


def test_both_sections_are_written_in_parallel_branches():
    """Two sections must produce two concurrent branches, not one sequential node."""
    _, events, _, _ = run([Review(approved=True)])
    assert events.count("write_section") == 2


def test_parallel_branches_merge_without_losing_each_other(sample_outline):
    """The `sections` reducer is what keeps concurrent writes from clobbering."""
    state, _, _, _ = run([Review(approved=True)])
    assert set(state["sections"]) == {"Executive Summary", "Outlook"}
    assert all(state["sections"].values())


def test_rejected_draft_loops_back_and_revises_only_the_flagged_section():
    rejection = Review(
        approved=False,
        issues=[Issue(severity="major", section="Outlook", problem="thin", fix="add detail")],
    )
    state, events, _, _ = run([rejection, Review(approved=True)])

    # `dispatch_revision` only routes and returns no state update, so it streams
    # no event of its own -- the second pass shows up as its dispatched work.
    assert events == [
        "research", "planning", "write_section", "write_section", "assemble", "reviewer",
        "write_section", "assemble", "reviewer",
    ]
    assert events.count("write_section") == 3, "pass 1 writes 2, pass 2 rewrites only 1"
    assert state["revision"] == 2
    assert state["review"].approved is True


def test_loop_terminates_when_the_revision_budget_runs_out():
    """A reviewer that never approves must not hang the graph."""
    rejection = Review(
        approved=False, issues=[Issue(severity="blocker", problem="wrong", fix="fix it")]
    )
    state, events, _, _ = run([rejection] * 10, max_revisions=2)

    assert events.count("assemble") == 2
    assert state["revision"] == 2
    assert state["review"].approved is False
    assert state["draft"], "an unapproved report is still published, not discarded"


def test_dispatch_routes_to_assemble_when_there_is_nothing_to_write(sample_outline):
    """An empty fan-out would strand the run, so it must route onward instead."""
    state = initial_state("Company X", "Q4")
    state.update(
        outline=sample_outline,
        sections={"Executive Summary": "a", "Competitive Position": "b"},
        review=Review(approved=False, issues=[]),
    )
    assert dispatch_sections(state) == ["assemble"]


def test_dispatch_emits_one_send_per_section_needing_work(sample_outline):
    state = initial_state("Company X", "Q4")
    state["outline"] = sample_outline

    sends = dispatch_sections(state)
    assert len(sends) == 2
    assert {s.node for s in sends} == {"write_section"}
    assert {s.arg["heading"] for s in sends} == {"Executive Summary", "Competitive Position"}


def test_trace_accumulates_across_the_whole_run():
    state, _, _, _ = run([Review(approved=False, issues=[Issue(severity="major", problem="p", fix="f")]),
                          Review(approved=True)])

    kinds = [line.split(":")[0] for line in state["trace"]]
    assert kinds == ["research", "planning", "writer", "reviewer", "writer", "reviewer"]


def test_sources_from_search_reach_the_final_state():
    source = Source(title="Q4 earnings", url="https://example.com/q4", snippet="revenue up")
    state, _, _, _ = run([Review(approved=True)], search=lambda q, n=5: [source])

    assert state["sources"] == [source]
    assert state["findings"][0].source_ids == [0]
