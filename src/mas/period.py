"""Validation for the reporting period.

The period was a free-form string pasted into every prompt, which caused two
real defects:

* The default was "Q4" with no year, so the model silently chose one.
* Nothing distinguished a fiscal quarter from a calendar one. A live Shopify
  run surfaced four different "Q1 2025" revenue figures and attributed them to
  "variations in data interpretation" -- when the likelier explanation is that
  they describe different periods entirely.

This does not build a fiscal calendar; that needs per-company data the system
does not have. It does the two things that are cheap and honest: insist on a
year, and make the agents state which period a figure belongs to rather than
assuming they all share one.
"""

from __future__ import annotations

import re

# Digit lookarounds rather than \b: a word boundary fails between the letter
# and digit in "FY2024", which is a perfectly ordinary way to name a period.
# The lookarounds still reject a year embedded in a longer number.
YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")

# Companies whose fiscal year is far enough from the calendar that "Q4 2025"
# is genuinely ambiguous. Not exhaustive -- a prompt hint, not a database.
KNOWN_OFFSET_FISCAL = {
    "nvidia": "fiscal year ends late January; FY2025 covers most of calendar 2024",
    "apple": "fiscal year ends late September",
    "microsoft": "fiscal year ends 30 June",
    "oracle": "fiscal year ends 31 May",
    "salesforce": "fiscal year ends 31 January",
    "broadcom": "fiscal year ends early November",
    "adobe": "fiscal year ends late November",
    "walmart": "fiscal year ends 31 January",
}


class InvalidPeriod(ValueError):
    """Raised when the reporting period is too vague to research."""


def validate(period: str) -> str:
    """Return the period unchanged, or raise if it names no year.

    A period without a year makes every downstream figure unverifiable: the
    model picks a year silently and the report never says which.
    """
    period = (period or "").strip()
    if not period:
        raise InvalidPeriod("No reporting period given. Try --quarter 'Q1 2025'.")

    if not YEAR.search(period):
        raise InvalidPeriod(
            f"Reporting period {period!r} has no year, so the agents would have to "
            f"guess one and the report would never say which they chose.\n"
            f"Add a year, e.g. --quarter '{period} 2025'."
        )
    return period


def fiscal_hint(company: str) -> str:
    """A prompt note when a company's fiscal year is offset from the calendar.

    Empty for companies not in the list, which is most of them -- the point is
    to warn where the ambiguity is known, not to pretend at full coverage.
    """
    note = KNOWN_OFFSET_FISCAL.get(company.strip().lower())
    if not note:
        return ""
    return (
        f"NOTE: {company}'s fiscal year is offset from the calendar year "
        f"({note}). A figure labelled for a fiscal period is NOT interchangeable "
        f"with the same-numbered calendar period. State which basis a figure "
        f"uses whenever a source makes it clear, and never merge the two."
    )


PERIOD_DISCIPLINE = """PERIOD DISCIPLINE — the requested period is {period}:
- A figure is only about {period} if its source says so. Sources routinely
  report adjacent quarters, trailing-twelve-month totals and full years.
- If sources give different values for what looks like the same metric, treat
  a period mismatch as the likeliest explanation before concluding the sources
  disagree. Say which period each figure covers.
- Never average, merge or silently pick between figures from different periods.
{fiscal}"""


def discipline(company: str, period: str) -> str:
    """The period-handling block injected into the research and writing prompts."""
    return PERIOD_DISCIPLINE.format(period=period, fiscal=fiscal_hint(company)).strip()
