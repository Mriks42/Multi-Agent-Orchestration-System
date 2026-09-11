"""Tests for the mechanical figure check.

Built before the check was wired into the Reviewer, because the risk here is not
that it misses fabrications but that it invents work: a false positive costs the
writer a revision it needed for a real defect. Most of what follows pins down
what must *not* be flagged.
"""

from __future__ import annotations

from mas.agents.figures import check_figures, extract_figures
from mas.state import Finding, Source


def _finding(claim: str) -> Finding:
    return Finding(claim=claim, topic="financials", source_ids=[0])


# ------------------------------------------------------------- extraction


def test_extracts_currency_percent_and_magnitude():
    figures = extract_figures("Revenue was $2.36 billion, up 27% from last year.")
    assert [(f.kind, f.value) for f in figures] == [
        ("quantity", 2.36e9),
        ("percent", 27.0),
    ]


def test_skips_bare_integers_years_and_quarter_labels():
    """The check is about claims, not about every digit on the page."""
    assert extract_figures("In Q1 2025 the company ran 3 segments across 12 markets") == []


def test_thousands_separator_makes_a_bare_number_material():
    figures = extract_figures("It opened 1,250 stores")
    assert [f.value for f in figures] == [1250.0]


def test_magnitude_letter_is_not_read_out_of_an_adjacent_word():
    """Without a word boundary, "5 bank" parses as five billion."""
    assert extract_figures("across 5 bank branches and 4 major terminals") == []


def test_basis_points_are_percent_scaled():
    figures = extract_figures("margin widened 150 bps")
    assert figures[0].kind == "percent"
    assert figures[0].value == 1.5


def test_citation_markers_are_not_figures():
    assert extract_figures("as reported [12] and [7]") == []


# --------------------------------------------------- matching and tolerance


def test_rounding_matches_in_both_directions():
    """A draft may round the evidence, and evidence may be rounder than the draft."""
    rounded = extract_figures("$11.6 billion")[0]
    precise = extract_figures("$11.63 billion")[0]
    assert rounded.matches(precise)
    assert precise.matches(rounded)


def test_a_different_number_does_not_match():
    assert not extract_figures("38%")[0].matches(extract_figures("31%")[0])


def test_percent_never_matches_a_quantity_of_the_same_size():
    assert not extract_figures("38%")[0].matches(extract_figures("38 million")[0])


# ------------------------------------------------------------ check_figures


def test_the_shopify_case_is_caught():
    """The live failure this check exists for: a percentage no finding contained."""
    findings = [_finding("GMV reached $60.9 billion in the quarter")]
    sources = [Source(title="Shopify Q1", snippet="GMV of $60.9 billion")]

    issues = check_figures("GMV increased by 38% to $60.9 billion.", findings, sources)

    assert len(issues) == 1
    assert "38%" in issues[0].problem
    assert issues[0].severity == "major", "derived figures exist, so this cannot be a blocker"


def test_format_mismatch_is_not_flagged():
    """"$11.6B" in the draft against "$11.63 billion" in a finding is the same fact."""
    findings = [_finding("Revenue of $11.63 billion")]
    assert check_figures("Revenue was $11.6B.", findings, []) == []


def test_a_figure_from_a_source_snippet_counts_as_grounded():
    """The writer is shown the sources too, so quoting one is not invention."""
    sources = [Source(title="Q1 release", snippet="operating margin of 14.2%")]
    assert check_figures("Operating margin stood at 14.2%.", [], sources) == []


def test_a_repeated_invented_figure_is_reported_once():
    findings = [_finding("Revenue grew year over year")]
    issues = check_figures("Up 38%. Later, still up 38%. Also 38%.", findings, [])
    assert len(issues) == 1


def test_the_generated_provenance_footer_is_not_checked():
    """Its counts come from the state, so they are grounded by construction."""
    draft = "Revenue grew.\n\n---\n\n## Provenance\n\n3 of 12 findings are backed by 45%."
    assert check_figures(draft, [_finding("Revenue grew")], []) == []


def test_many_invented_figures_collapse_into_a_summary_issue():
    draft = " ".join(f"metric {n}% rose" for n in range(11, 30))
    issues = check_figures(draft, [_finding("nothing numeric here")], [])
    assert len(issues) == 7, "six individual issues plus one summarising the rest"
    assert "further figure(s)" in issues[-1].problem


def test_no_findings_and_no_sources_flags_every_figure():
    """The --no-search path: nothing is grounded, and the check should say so."""
    issues = check_figures("Revenue was $3 billion, up 12%.", [], [])
    assert len(issues) == 2
