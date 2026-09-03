import pytest

from mas.graph import route_after_review
from mas.state import Issue, Review


def _review(approved: bool) -> Review:
    return Review(
        approved=approved,
        issues=[] if approved else [Issue(severity="blocker", problem="p", fix="f")],
    )


def test_approved_draft_publishes():
    assert route_after_review({"review": _review(True), "revision": 1, "max_revisions": 2}) == "publish"


def test_rejected_draft_within_budget_revises():
    assert route_after_review({"review": _review(False), "revision": 1, "max_revisions": 2}) == "revise"


@pytest.mark.parametrize("revision", [2, 3])
def test_rejected_draft_at_or_past_budget_publishes(revision):
    state = {"review": _review(False), "revision": revision, "max_revisions": 2}
    assert route_after_review(state) == "publish"


def test_missing_review_publishes_rather_than_looping():
    assert route_after_review({"revision": 1, "max_revisions": 2}) == "publish"
