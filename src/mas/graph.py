"""The orchestration graph.

    START -> research -> planning -> writer -> reviewer -> END
                                       ^          |
                                       +--revise--+

The one cycle is the review loop: the Reviewer sends the draft back to the
Writer until it approves or the revision budget runs out. `max_revisions` is what
makes that loop provably terminate.
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph

from .agents import (
    make_planning_node,
    make_research_node,
    make_reviewer_node,
    make_writer_node,
)
from .deps import Deps
from .state import ReportState, initial_state

log = logging.getLogger(__name__)


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


def build_graph(deps: Deps, checkpointer=None):
    """Wire the four agents into a compiled LangGraph application."""
    graph = StateGraph(ReportState)

    graph.add_node("research", make_research_node(deps))
    graph.add_node("planning", make_planning_node(deps))
    graph.add_node("writer", make_writer_node(deps))
    graph.add_node("reviewer", make_reviewer_node(deps))

    graph.add_edge(START, "research")
    graph.add_edge("research", "planning")
    graph.add_edge("planning", "writer")
    graph.add_edge("writer", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"revise": "writer", "publish": END},
    )

    return graph.compile(checkpointer=checkpointer)


def run_report(
    deps: Deps,
    company: str,
    quarter: str,
    focus: str = "",
    max_revisions: int | None = None,
    on_event=None,
) -> ReportState:
    """Run one report end to end and return the final state.

    `on_event(node, update)` is called after each agent finishes, which is what
    the CLI uses to show live progress.
    """
    app = build_graph(deps)
    state = initial_state(
        company,
        quarter,
        focus,
        max_revisions if max_revisions is not None else deps.settings.max_revisions,
    )

    final: ReportState = state
    # `stream` yields one {node_name: update} dict per completed node, and the
    # recursion limit is a hard stop in case a future edge change reopens a loop.
    for chunk in app.stream(state, {"recursion_limit": 50}, stream_mode="updates"):
        for node, update in chunk.items():
            if on_event:
                on_event(node, update)
            final = {**final, **update, "trace": final.get("trace", []) + update.get("trace", [])}
    return final
