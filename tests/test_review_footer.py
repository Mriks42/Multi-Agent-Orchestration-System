"""Tests for the review verdict that ships with the report.

The report is published whether or not the Reviewer signed off. These pin down
that the artifact says so, and -- just as important -- that saying so cannot
move an eval metric, since the block quotes the Reviewer verbatim and reviewers
write things like "the claim at [1] is unsupported".
"""

from __future__ import annotations

from mas.agents.writer import REVIEW_HEADING, review_footer, stamp_review
from mas.evals.metrics import score_run
from mas.state import Finding, Issue, Review, Source


def _review(approved: bool, *issues: Issue) -> Review:
    return Review(approved=approved, issues=list(issues))


def test_an_unapproved_report_says_so_and_names_the_issues():
    review = _review(
        False,
        Issue(severity="blocker", section="Financials", problem="Invented a figure.", fix="Cut it."),
        Issue(severity="major", section="", problem="Outlook has no substance.", fix="Add some."),
    )
    footer = review_footer(review)

    assert "**Published with 2 unresolved issue(s)**" in footer
    assert "1 blocker, 1 major" in footer
    assert "did not sign off" in footer
    assert "Invented a figure." in footer
    assert "Outlook has no substance." in footer


def test_an_issue_with_no_section_is_attributed_to_the_report():
    footer = review_footer(_review(False, Issue(severity="blocker", problem="p", fix="f")))
    assert "**blocker** — report: p" in footer


def test_an_approved_report_says_that_instead():
    footer = review_footer(_review(True))
    assert "**Approved.**" in footer
    assert "unresolved" not in footer


def test_minor_issues_are_counted_but_not_listed():
    """They do not block approval, so they are noise in a published disclosure."""
    review = _review(
        False,
        Issue(severity="blocker", section="", problem="real problem", fix="f"),
        Issue(severity="minor", section="", problem="comma splice", fix="f"),
    )
    footer = review_footer(review)

    assert "1 minor" in footer
    assert "comma splice" not in footer


def test_the_caveat_travels_with_every_verdict():
    """Approval means the findings back the draft, not that the draft is true."""
    for review in (_review(True), _review(False, Issue(severity="blocker", problem="p", fix="f")), None):
        assert "not against reality" in review_footer(review)


def test_an_unreviewed_draft_is_labelled_as_such():
    assert "was not reviewed" in review_footer(None)


# ------------------------------------------------------------------ stamping


def test_stamping_is_idempotent():
    """A resumed run must not append a second verdict."""
    once = stamp_review("# Report\n", _review(True))
    assert stamp_review(once, _review(True)) == once
    assert once.count(REVIEW_HEADING) == 1


def test_an_empty_draft_is_left_alone():
    assert stamp_review("", _review(True)) == ""


# ------------------------------------------- the disclosure must not be scored


def test_quoted_citations_in_the_verdict_do_not_become_citations():
    """The reason every metric reads _body: reviewers quote citation markers."""
    draft = (
        "# R\n\n## Financials\n\nRevenue rose [0].\n\n"
        "---\n\n## Provenance\n\n1 of 1 findings are backed by a retrieved source.\n"
    )
    review = _review(
        False,
        Issue(severity="blocker", section="", problem="The claim at [1] and [47] is bogus.", fix="f"),
    )
    state = {
        "draft": stamp_review(draft, review),
        "findings": [Finding(claim="c", topic="t", source_ids=[0])],
        "sources": [Source(title="s")],
        "review": review,
    }

    metrics = score_run(state)

    assert metrics.citation_count == 1, "only the citation in the report body counts"
    assert metrics.orphan_citation_count == 0, "[47] was quoted by the reviewer, not cited"
    assert metrics.distinct_sources_cited == 1


def test_the_verdict_does_not_inflate_the_word_or_section_count():
    draft = "# R\n\n## One\n\nText.\n\n---\n\n## Provenance\n\nAll sourced.\n"
    bare = score_run({"draft": draft})
    stamped = score_run({"draft": stamp_review(draft, _review(False, Issue(
        severity="blocker", section="", problem="a much longer problem statement", fix="f")))})

    assert stamped.section_count == bare.section_count
    assert stamped.word_count == bare.word_count
