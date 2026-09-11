"""Mechanical check that every material figure in the draft came from the evidence.

The writer's remaining failure mode is invented numbers: a live Shopify run with
13 sourced findings and 22 sources produced "GMV increased by 38%", a figure no
finding contained. The Reviewer is asked to catch exactly that and does not do it
reliably -- the same reason `check_citations` exists. Anything code can settle
exactly should not be delegated to a model that settles it sometimes.

Scope is deliberately narrow: only *material* figures are checked -- currency
amounts, percentages, and magnitudes. Those carry the factual weight and are
what gets invented. Bare counts ("three segments"), years and quarter labels are
skipped, because flagging them buys nothing and every false positive costs the
writer a revision it could have spent on a real defect.

Two false-positive classes shaped the design:

* **Format mismatch** -- "$11.6 billion" in the draft against "$11.63B" in a
  finding. Solved: figures are parsed to a number and compared with a tolerance
  taken from the written precision, so rounding matches and invention does not.
* **Derived figures** -- "up 31%", computed from two findings that each give a
  level but not the delta. Matching cannot solve this, so findings are raised as
  "major" rather than "blocker" and the fix tells the writer to show the
  derivation. This check reduces fabrication; it does not end it.

The haystack is the findings *and* the sources, because the Writer is shown
both. A figure quoted from a source snippet is properly grounded even when no
finding distilled it, and flagging it would be a false positive. Whether a
grounded figure is *attributed* is a separate question, handled by the prompt's
UNSOURCED rules and measured by `unattributed_figure_count`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..state import Issue

# Multipliers for magnitude suffixes. Single letters are allowed because drafts
# use them ("$11.6B"), and are safe only with the trailing \b in _FIGURE below:
# without it the "b" in "5 bank branches" would read as five billion.
_MAGNITUDES = {
    "trillion": 1e12, "tn": 1e12, "t": 1e12,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mm": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
}

_FIGURE = re.compile(
    r"""
    (?P<currency>[$€£])?\s*
    (?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<magnitude>trillion|billion|million|thousand|bn|tn|mm|[btmk])?\b
    \s*
    (?P<percent>%|percentage\s+points?|percent|bps|basis\s+points?)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Citation markers are stripped before parsing: "[12]" is an index into the
# source list, not a quantity, and reads as one to any number-matching regex.
_CITATION = re.compile(r"\[\d+\]")

# The Provenance footer is generated from the state rather than written by the
# model, so its counts are grounded by construction and must not be checked.
_FOOTER = re.compile(r"\n-{3,}\s*\n+##\s*Provenance\b")

# How many distinct ungrounded figures to report. A draft full of invented
# numbers needs a rewrite, not forty issues; the message says how many remain.
_MAX_ISSUES = 6


@dataclass(frozen=True)
class Figure:
    """One parsed figure: what it means, not how it was written.

    `tolerance` is half the last significant digit as written, which is what
    makes rounding legitimate -- "$11.6B" tolerates anything that rounds to it,
    while "$11.63B" demands the tighter number the extra digit claims.
    """

    kind: str  # "percent" or "quantity" -- 38% must never match 38 million
    value: float
    tolerance: float
    text: str  # as it appears, so the issue can quote the draft

    def matches(self, other: "Figure") -> bool:
        """True when the two could be the same number written to either precision."""
        if self.kind != other.kind:
            return False
        allowed = max(self.tolerance, other.tolerance) + 1e-9
        return abs(self.value - other.value) <= allowed


def _body(text: str) -> str:
    """The report without its generated Provenance footer."""
    return _FOOTER.split(text)[0]


def extract_figures(text: str) -> list[Figure]:
    """Parse every material figure in `text`.

    Material means the number carries a unit that makes it a claim: a currency
    symbol, a percentage, a magnitude word, or a thousands separator. A bare
    integer is skipped, which is what keeps years ("2025"), quarter labels
    ("Q1") and counts ("3 segments") out of the check.
    """
    figures = []
    for m in _FIGURE.finditer(_CITATION.sub(" ", text)):
        number, magnitude, percent = m.group("number"), m.group("magnitude"), m.group("percent")

        if not (m.group("currency") or percent or magnitude or "," in number):
            continue

        multiplier = _MAGNITUDES[magnitude.lower()] if magnitude else 1.0
        kind = "quantity"
        if percent:
            kind = "percent"
            # 100 bps is 1%, so basis points are percent scaled by a hundredth.
            # Rescaling rather than adding a third kind lets a draft's "100 bps"
            # match a finding's "1%", which is the same fact either way.
            if percent.lower().startswith(("bps", "basis")):
                multiplier = 0.01

        decimals = len(number.partition(".")[2])
        unit = multiplier * (10.0 ** -decimals)
        figures.append(
            Figure(
                kind=kind,
                value=float(number.replace(",", "")) * multiplier,
                tolerance=unit / 2,
                text=m.group(0).strip(),
            )
        )
    return figures


def _evidence_text(findings, sources) -> str:
    """Everything the Writer was shown that could legitimately carry a figure."""
    parts = [f.claim for f in findings]
    parts += [f"{s.title} {s.snippet}" for s in sources]
    return "\n".join(parts)


def check_figures(draft: str, findings, sources) -> list[Issue]:
    """Flag material figures in the draft that appear nowhere in the evidence.

    Deliberately not a blocker: derived figures are legitimate and cannot be
    matched, so a blocker here would stall reports on correct arithmetic. The
    fix offers the writer all three honest routes out.
    """
    evidence = extract_figures(_evidence_text(findings, sources))

    # Distinct by meaning, not spelling: a figure repeated in three sections is
    # one defect, and the writer should not be told about it three times.
    ungrounded: list[Figure] = []
    for figure in extract_figures(_body(draft)):
        if any(figure.matches(known) for known in evidence):
            continue
        if any(figure.matches(seen) for seen in ungrounded):
            continue
        ungrounded.append(figure)

    issues = [
        Issue(
            severity="major",
            section="",
            problem=f"The figure \"{figure.text}\" does not appear in any finding or "
                    f"source. Either it was derived from the evidence, or it was invented.",
            fix=f"If \"{figure.text}\" follows from the findings, say which ones and how "
                f"in the sentence itself. If it came from a source, cite it. Otherwise "
                f"remove the figure and the claim that rests on it.",
        )
        for figure in ungrounded[:_MAX_ISSUES]
    ]
    if len(ungrounded) > _MAX_ISSUES:
        remaining = len(ungrounded) - _MAX_ISSUES
        issues.append(
            Issue(
                severity="major",
                section="",
                problem=f"{remaining} further figure(s) in the draft appear in no finding "
                        f"or source: {', '.join(f.text for f in ungrounded[_MAX_ISSUES:])}.",
                fix="Ground each against a cited source, show the derivation, or remove it.",
            )
        )
    return issues
