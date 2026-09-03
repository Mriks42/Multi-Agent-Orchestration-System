from mas.state import Issue, Review, initial_state


def test_initial_state_has_every_key_agents_read():
    state = initial_state("Company X", "Q4", focus="pricing", max_revisions=3)
    assert state["company"] == "Company X"
    assert state["focus"] == "pricing"
    assert state["max_revisions"] == 3
    assert state["revision"] == 0
    assert state["sources"] == [] and state["findings"] == [] and state["trace"] == []


def test_blockers_filters_by_severity():
    review = Review(
        approved=False,
        issues=[
            Issue(severity="blocker", problem="invented figure", fix="remove it"),
            Issue(severity="minor", problem="wordy", fix="trim"),
        ],
    )
    assert [i.problem for i in review.blockers] == ["invented figure"]
