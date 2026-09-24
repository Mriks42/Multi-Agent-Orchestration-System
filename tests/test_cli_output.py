"""What the CLI actually prints, as opposed to what it looks like it prints.

`_summarise` renders through rich, which treats square brackets as markup. The
severity is written in brackets and the section, problem and summary all come
from the model, so every one of them is markup unless escaped. "[blocker]" was
parsed as a style tag and silently dropped: each run printed its issues with no
severity at all, and the README documented sample output the code could not
produce. Nothing caught it because the markdown report footer renders severity
correctly, which is where it was being read.

These assert on rendered text, not on the f-string, because the f-string looked
right the whole time it was wrong.
"""

from __future__ import annotations

import pytest

from mas import cli
from mas.state import Issue, Review


def render(review: Review) -> str:
    """The text a user would see on the terminal for this review."""
    with cli.console.capture() as captured:
        cli._summarise({"review": review})
    return captured.get()


@pytest.mark.parametrize("severity", ["blocker", "major", "minor"])
def test_severity_survives_rendering(severity):
    """The severity must reach the terminal, not be eaten as a style tag."""
    review = Review(
        approved=False,
        issues=[Issue(severity=severity, section="Outlook", problem="p", fix="f")],
    )
    assert f"[{severity}]" in render(review)


def test_model_text_with_brackets_is_not_swallowed():
    """Problem text is model-written and may contain brackets of its own.

    A citation marker like [2] is the likeliest case, and losing it would drop
    exactly the evidence pointer the issue is about.
    """
    review = Review(
        approved=False,
        issues=[
            Issue(
                severity="major",
                section="Financial Performance",
                problem="the $5.4bn figure cited as [2] conflicts with [3]",
                fix="reconcile them",
            )
        ],
        summary="Totals disagree between [2] and [3].",
    )
    out = render(review)
    assert "[2]" in out and "[3]" in out
    assert "[major]" in out
    assert "Totals disagree between [2] and [3]." in out


def test_sectionless_issue_is_labelled_report():
    """`check_figures` raises issues with no section; they must still read."""
    review = Review(
        approved=False,
        issues=[Issue(severity="major", section="", problem="p", fix="f")],
    )
    assert "report:" in render(review)


def test_approved_review_says_so():
    assert "approved" in render(Review(approved=True)).lower()
