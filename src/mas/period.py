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
    # Added after the list was caught being wrong about its own eval set: these
    # three are cases in `evals/cases.py` and all three run offset years, so
    # every stored eval has quietly asked them an ambiguous question.
    "snowflake": "fiscal year ends 31 January",
    "zscaler": "fiscal year ends 31 July",
    "braze": "fiscal year ends 31 January",
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


# "Q1 FY2025" and "Q1 calendar 2025" are already unambiguous, so saying the
# period could mean two things would be wrong as well as annoying.
_EXPLICIT_BASIS = re.compile(r"\b(FY|CY|fiscal|calendar)", re.IGNORECASE)


def ambiguity_warning(company: str, period: str) -> str:
    """A warning for the person asking, when their period names two quarters.

    `fiscal_hint` tells the *model* that a company's year is offset. This tells
    the *user*, who is the only party that can actually resolve it -- and until
    it existed, nobody did. A live NVIDIA run for "Q1 2025" drew $26.0bn (fiscal
    Q1 2025, ending April 2024) and $44.1bn (calendar Q1 2025) from the sources
    and presented both as the same quarter. Both figures were real and cited;
    the question was ambiguous and nothing said so.

    Empty when the company keeps a calendar year, or when the period already
    states its basis.
    """
    company = (company or "").strip()
    period = (period or "").strip()

    if not company or not period:
        return ""

    note = KNOWN_OFFSET_FISCAL.get(company.lower())
    if not note or _EXPLICIT_BASIS.search(period):
        return ""

    lead = (
        f"{company}'s {note}. So \"{period}\" could mean the fiscal quarter or "
        f"the calendar one, and they are different periods — for {company} they "
        f"can be a year apart. "
    )

    # "Q1 2025" -> "Q1 FY2025", which is how these are actually written. A
    # period with no year cannot be rewritten this way, but `validate` rejects
    # that separately, so fall back rather than printing a broken example.
    if not YEAR.search(period):
        return lead + 'Name the basis explicitly to say which you mean.'

    return lead + (
        f'Write "{YEAR.sub(lambda m: "FY" + m.group(0), period)}" or '
        f'"{YEAR.sub(lambda m: "CY" + m.group(0), period)}" to say which you mean.'
    )


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
