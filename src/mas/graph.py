"""The orchestration graph.

                              ┌─ write_section ─┐
    START -> research -> planning -> write_section -> assemble -> reviewer -> END
                              └─ write_section ─┘        ^           │
                                                         └─ revise ──┘

Two structures matter here. The fan-out: sections are independent LLM calls, so
`plan_sections` emits one `Send` per section and they run as concurrent
branches, merging through the `sections` reducer. And the cycle: the Reviewer
sends the draft back for revision until it approves or the revision budget runs
out, which is what makes the loop provably terminate.
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .agents import (
    make_assemble_node,
    make_planning_node,
    make_research_node,
    make_reviewer_node,
    make_write_section_node,
    plan_sections,
    stamp_review,
)
from .deps import Deps
from .state import ReportState, initial_state

log = logging.getLogger(__name__)


def dispatch_sections(state: ReportState) -> list:
    """Fan out one branch per section needing work.

    Returning node names instead of `Send` objects when there is nothing to
    write keeps the graph moving: an empty list would strand the run.
    """
    tasks = plan_sections(state)
    if not tasks:
        return ["assemble"]
    log.info("dispatch: %d section(s) in parallel", len(tasks))
    return [Send("write_section", task) for task in tasks]


def route_after_assemble(state: ReportState) -> Literal["draft_more", "review"]:
    """Send an unfinished draft back for its next wave before reviewing it.

    Drafting is two waves: body sections, then the sections that summarise them.
    Reviewing a half-written report would waste a reviewer call and, worse,
    spend a revision on sections that were never drafted.
    """
    outline = state.get("outline")
    if outline is None:
        return "review"
    sections = state.get("sections", {})
    if all(sections.get(s.heading) for s in outline.sections):
        return "review"
    log.info("draft incomplete; dispatching the next wave")
    return "draft_more"


def route_after_review(state: ReportState) -> Literal["revise", "publish"]:
    """Decide whether the draft goes back to the Writer or ships."""
    review = state.get("review")
    if review is None or review.approved:
        return "publish"
    if state.get("revision", 0) >= state.get("max_revisions", 2):
        log.warning("revision budget exhausted; publishing with %d open issue(s)",
                    len(review.issues))
        return "publish"
    return "revise"


def make_distributed_node(deps: Deps):
    """Build the node that farms sections out to worker processes.

    The distributed and in-process paths write the same `sections` update, so
    every downstream node -- and every test of them -- is unchanged by the
    choice. Only the dispatch mechanism differs.
    """
    import uuid

    def distribute(state: ReportState) -> dict:
        tasks = plan_sections(state)
        if not tasks:
            return {}

        # Issues are pydantic objects; workers receive JSON, so flatten them.
        payloads = [{**t, "issues": [i.model_dump() for i in t["issues"]]} for t in tasks]
        run_id = f"{state['company']}-{state.get('revision', 0)}-{uuid.uuid4().hex[:8]}"

        deps.broker.submit(run_id, payloads)
        log.info("dispatched %d section(s) to workers as run %s", len(payloads), run_id)
        results = deps.broker.gather(run_id, timeout=deps.settings.task_timeout)

        sections: dict[str, str] = {}
        for result in results.values():
            sections.update(result.get("sections", {}))
        return {"sections": sections}

    return distribute


def build_graph(deps: Deps, checkpointer=None):
    """Wire the agents into a compiled LangGraph application.

    Two topologies share every node but the dispatch step: with a broker,
    sections go to worker processes; without one, they fan out in-process.
    """
    graph = StateGraph(ReportState)
    distributed = deps.broker is not None

    graph.add_node("research", make_research_node(deps))
    graph.add_node("planning", make_planning_node(deps))
    graph.add_node("assemble", make_assemble_node(deps))
    graph.add_node("reviewer", make_reviewer_node(deps))

    graph.add_edge(START, "research")
    graph.add_edge("research", "planning")

    if distributed:
        graph.add_node("distribute", make_distributed_node(deps))
        graph.add_edge("planning", "distribute")
        graph.add_edge("distribute", "assemble")
        next_wave = "distribute"
    else:
        graph.add_node("write_section", make_write_section_node(deps))
        graph.add_edge("write_section", "assemble")
        # Fan out from planning, and again for the second drafting wave or a
        # revision pass -- all three go through the same dispatch.
        graph.add_conditional_edges("planning", dispatch_sections, ["write_section", "assemble"])
        graph.add_node("dispatch_revision", lambda state: {})
        graph.add_conditional_edges(
            "dispatch_revision", dispatch_sections, ["write_section", "assemble"]
        )
        next_wave = "dispatch_revision"

    # An incomplete draft goes back for its next wave rather than to review.
    graph.add_conditional_edges(
        "assemble",
        route_after_assemble,
        {"draft_more": next_wave, "review": "reviewer"},
    )
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"revise": next_wave, "publish": END},
    )

    return graph.compile(checkpointer=checkpointer)


def run_report(
    deps: Deps,
    company: str,
    quarter: str,
    focus: str = "",
    max_revisions: int | None = None,
    on_event=None,
    checkpointer=None,
    thread_id: str | None = None,
) -> ReportState:
    """Run one report end to end and return the final state.

    `on_event(node, update)` is called after each agent finishes, which is what
    the CLI uses to show live progress.

    With a `checkpointer` and `thread_id`, state is persisted after every node.
    Passing a thread that already has saved state resumes it: LangGraph is given
    `None` as input, which means "continue from the last checkpoint" rather than
    "start again".
    """
    app = build_graph(deps, checkpointer=checkpointer)
    config = {"recursion_limit": 50}

    state: ReportState | None = initial_state(
        company,
        quarter,
        focus,
        max_revisions if max_revisions is not None else deps.settings.max_revisions,
    )
    final: ReportState = dict(state)

    if checkpointer is not None and thread_id:
        config["configurable"] = {"thread_id": thread_id}
        saved = app.get_state(config)
        if saved and saved.values:
            log.info("resuming thread %s", thread_id)
            final = dict(saved.values)
            state = None  # None tells LangGraph to continue, not restart

    # `stream` yields one {node_name: update} dict per completed node, and the
    # recursion limit is a hard stop in case a future edge change reopens a loop.
    for chunk in app.stream(state, config, stream_mode="updates"):
        for node, update in chunk.items():
            if not isinstance(update, dict):
                continue
            if on_event:
                on_event(node, update)
            merged = {**final, **update, "trace": final.get("trace", []) + update.get("trace", [])}
            # `sections` has a reducer in the graph; mirror it here so the
            # locally tracked copy does not lose parallel branches' work.
            if "sections" in update:
                merged["sections"] = {**final.get("sections", {}), **update["sections"]}
            final = merged

    # The verdict is stamped here, not in `assemble`, because assemble runs
    # before the review that judges it. Every caller -- CLI, web and evals --
    # comes through this function, so the published artifact always carries it.
    final["draft"] = stamp_review(final.get("draft", ""), final.get("review"))
    return final
