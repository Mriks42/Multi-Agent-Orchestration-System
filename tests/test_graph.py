"""End-to-end graph tests: all four agents, no network."""

from conftest import FakeChatModel, make_deps

from mas.graph import run_report
from mas.state import Finding, Issue, Outline, Review, Section, Source


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

    assert events == ["research", "planning", "writer", "reviewer"]
    assert state["review"].approved is True
    assert state["revision"] == 1
    assert "# Company X — Q4 Market Research" in state["draft"]
    assert "## Outlook" in state["draft"]
    assert len(reviewer.calls) == 1


def test_rejected_draft_loops_back_to_the_writer_then_ships():
    rejection = Review(
        approved=False,
        issues=[Issue(severity="major", section="Outlook", problem="thin", fix="add detail")],
    )
    state, events, _, _ = run([rejection, Review(approved=True)])

    assert events == ["research", "planning", "writer", "reviewer", "writer", "reviewer"]
    assert state["revision"] == 2
    assert state["review"].approved is True


def test_loop_terminates_when_the_revision_budget_runs_out():
    """A reviewer that never approves must not hang the graph."""
    rejection = Review(
        approved=False, issues=[Issue(severity="blocker", problem="wrong", fix="fix it")]
    )
    state, events, _, _ = run([rejection] * 10, max_revisions=2)

    assert events.count("writer") == 2
    assert state["revision"] == 2
    assert state["review"].approved is False
    assert state["draft"], "an unapproved report is still published, not discarded"


def test_trace_accumulates_one_line_per_agent_across_the_whole_run():
    state, _, _, _ = run([Review(approved=False, issues=[Issue(severity="major", problem="p", fix="f")]),
                          Review(approved=True)])

    kinds = [line.split(":")[0] for line in state["trace"]]
    assert kinds == ["research", "planning", "writer", "reviewer", "writer", "reviewer"]


def test_sources_from_search_reach_the_final_state():
    source = Source(title="Q4 earnings", url="https://example.com/q4", snippet="revenue up")
    state, _, _, _ = run([Review(approved=True)], search=lambda q, n=5: [source])

    assert state["sources"] == [source]
    assert state["findings"][0].source_ids == [0]
