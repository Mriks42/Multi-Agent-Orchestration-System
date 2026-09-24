"""The HTTP layer, driven end to end against a scripted model.

The whole pipeline runs here -- four agents, the revision loop, the provenance
count -- just over HTTP instead of a terminal, and with no API key.
"""

from __future__ import annotations

import time

import pytest
from conftest import FakeChatModel, make_deps
from fastapi.testclient import TestClient

from mas.state import Finding, Issue, Outline, Review, Section, Source
from mas.web import JobStore, create_app


SOURCE = Source(title="Q1 earnings", url="https://example.com/q1", snippet="revenue")


def build_models(reviews=None):
    reviews = iter(reviews or [Review(approved=True)])

    def handler(schema, messages, model):
        name = schema.__name__
        if name == "_Queries":
            return schema(queries=["q1"])
        if name == "_Findings":
            return schema(findings=[
                Finding(claim="Revenue was $2.1B", topic="fin", source_ids=[0]),
                Finding(claim="Headcount near 4000", topic="ops"),  # unsourced
            ])
        if name == "Outline":
            return Outline(title="Acme — Q1 2025", sections=[
                Section(heading="Financial Performance", purpose="numbers"),
                Section(heading="Executive Summary", purpose="frame", synthesises=True),
            ])
        raise AssertionError(name)

    writer = FakeChatModel(
        handlers={k: handler for k in ("_Queries", "_Findings", "Outline")},
        text_handler=lambda messages, m: "section body [0]",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: next(reviews)})
    return writer, reviewer


@pytest.fixture
def client():
    writer, reviewer = build_models()
    deps = make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE])
    return TestClient(create_app(deps=deps, store=JobStore(), max_revisions=1))


def wait_for(client, job_id, timeout=10.0):
    """Poll until the job finishes, as the page does."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/reports/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {body}")


# ------------------------------------------------------------------ the page


def test_the_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Multi-Agent Research" in response.text


def test_health_reports_the_models_in_use(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["model"] and body["reviewer_model"]


# --------------------------------------------------------------- submit/poll


def test_submitting_returns_a_job_id_immediately(client):
    response = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"})
    assert response.status_code == 202, "the report takes ~25s; the request must not block"
    assert response.json()["id"]


def test_a_submitted_report_runs_to_completion(client):
    job_id = client.post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    ).json()["id"]

    body = wait_for(client, job_id)
    assert body["status"] == "done"
    assert "# Acme — Q1 2025" in body["report"]
    assert "## Financial Performance" in body["report"]


def test_progress_names_each_agent_as_it_finishes(client):
    """The four agents are the point; a spinner would hide them.

    The Writer reports twice because drafting is two waves -- bodies first, then
    the summary that reads them.
    """
    job_id = client.post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    ).json()["id"]

    agents = [s["agent"] for s in wait_for(client, job_id)["steps"]]
    assert agents == ["Research Agent", "Planning Agent",
                      "Writer Agent", "Writer Agent", "Reviewer Agent"]
    assert agents[0] == "Research Agent" and agents[-1] == "Reviewer Agent"


def test_provenance_counts_reach_the_page(client):
    """A reader must be able to see how much of the report is actually sourced."""
    job_id = client.post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    ).json()["id"]

    prov = wait_for(client, job_id)["provenance"]
    assert prov == {"sourced": 1, "total": 2, "unsourced": 1}


def test_unresolved_issues_are_surfaced_not_hidden():
    """A report published with open issues must say so."""
    rejection = Review(approved=False, issues=[
        Issue(severity="blocker", section="Executive Summary",
              problem="invented figure", fix="remove it")])
    writer, reviewer = build_models([rejection, rejection])
    app = create_app(deps=make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE]),
                     store=JobStore(), max_revisions=1)
    client = TestClient(app)

    job_id = client.post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    ).json()["id"]

    body = wait_for(client, job_id)
    assert body["approved"] is False
    assert any("invented figure" in issue for issue in body["open_issues"])


# -------------------------------------------------------------------- errors


def test_a_period_without_a_year_is_rejected_before_any_work(client):
    response = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1"})
    assert response.status_code == 422
    assert "year" in response.json()["detail"]


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/reports/nope").status_code == 404


def test_a_failing_run_is_reported_not_swallowed():
    """Nothing is waiting to catch the exception, so it must land on the job."""
    writer, reviewer = build_models()
    writer.handlers["_Queries"] = lambda s, m, mo: (_ for _ in ()).throw(RuntimeError("api down"))
    client = TestClient(create_app(deps=make_deps(writer, reviewer), store=JobStore()))

    job_id = client.post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    ).json()["id"]

    body = wait_for(client, job_id)
    assert body["status"] == "failed"
    assert "api down" in body["error"]


def test_an_empty_company_is_rejected(client):
    assert client.post("/api/reports", json={"company": "", "quarter": "Q1 2025"}).status_code == 422


# -------------------------------------------------------------------- listing


def test_recent_reports_are_listed_newest_first(client):
    for company in ("First", "Second"):
        client.post("/api/reports", json={"company": company, "quarter": "Q1 2025"})

    companies = [r["company"] for r in client.get("/api/reports").json()["reports"]]
    assert companies[:2] == ["Second", "First"]


def test_the_job_store_is_bounded():
    """A long-running server must not grow without limit."""
    store = JobStore(max_jobs=3)
    ids = [store.create(f"C{i}", "Q1 2025").id for i in range(5)]

    assert store.get(ids[0]) is None, "the oldest job should have been evicted"
    assert store.get(ids[-1]) is not None
    assert len(store.recent(limit=100)) == 3


# ------------------------------------------------------------- page rendering


def test_the_report_is_passed_through_the_markdown_renderer():
    """Defining markdown() is not enough -- it has to be called.

    It was defined, styled and left uncalled, so the page dropped the
    `white-space: pre-wrap` that had been formatting the report and rendered
    nothing in its place: one wall of text with visible "##" markers.
    """
    script = _page_script()

    assert "function markdown(" in script, "the renderer must exist"
    assert "markdown(job.report)" in script, "and the report must go through it"
    assert "esc(job.report)" not in script, "the raw escape path must be gone"


def test_headings_are_converted_and_styled():
    page = __import__("pathlib").Path("src/mas/web/index.html").read_text(encoding="utf-8")

    assert 'b.startsWith("## ")' in page, "h2 headings must be converted"
    assert "article h2" in page, "and styled"


def test_the_page_does_not_repeat_the_provenance_section():
    """It is shown in its own card above the report; twice is noise."""
    page = (
        __import__("pathlib").Path("src/mas/web/index.html")
    ).read_text(encoding="utf-8")

    assert "Provenance" in page and "split" in page
    assert "already shown above" in page, "the reason should be recorded"


def _page_script() -> str:
    import pathlib

    page = pathlib.Path("src/mas/web/index.html").read_text(encoding="utf-8")
    return page.split("<script>")[1].split("</script>")[0]


def test_no_javascript_literal_is_split_across_lines():
    r"""String-presence tests stay green on broken JS; this catches what they miss.

    An editing slip turned the two-character sequence backslash-n inside two
    regex literals into real newlines, which breaks every regex after it. The
    page still contained every expected substring, so nothing else failed.

    A regex or template literal must open and close on the same line, so an odd
    count of backticks on a line means one is running on.
    """
    for number, line in enumerate(_page_script().splitlines(), 1):
        assert line.count("`") % 2 == 0, f"line {number} has an unclosed backtick: {line!r}"

    script = _page_script()
    assert "split(/\\n---" in script, "the provenance splitter must be on one line"
    assert "split(/\\n{2,}/)" in script, "the paragraph splitter must be on one line"


def test_the_reports_own_markdown_round_trips_through_the_renderer():
    """The renderer must handle the exact shape `_render` produces."""
    script = _page_script()

    # These are the three block kinds a report contains.
    assert 'b.startsWith("# ")' in script, "the title"
    assert 'b.startsWith("## ")' in script, "section headings"
    assert "cite" in script, "citations"


def test_report_text_is_escaped_before_being_rendered():
    """The model writes the report, so it is never trusted as HTML."""
    page = (
        __import__("pathlib").Path("src/mas/web/index.html")
    ).read_text(encoding="utf-8")

    assert "esc(b.slice(3))" in page
    assert "esc(b).replace" in page


def test_the_page_is_told_which_sections_were_drafted_in_parallel():
    """The fan-out is the project's subject; a flat step list would hide it."""
    def handler(schema, messages, model):
        name = schema.__name__
        if name == "_Queries":
            return schema(queries=["q1"])
        if name == "_Findings":
            return schema(findings=[Finding(claim="c", topic="t", source_ids=[0])])
        if name == "Outline":
            return Outline(title="Acme — Q1 2025", sections=[
                Section(heading="Financials", purpose="p"),
                Section(heading="Competition", purpose="p"),
                Section(heading="Risks", purpose="p"),
            ])
        raise AssertionError(name)

    writer = FakeChatModel(
        handlers={k: handler for k in ("_Queries", "_Findings", "Outline")},
        text_handler=lambda messages, m: "body [0]",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: Review(approved=True)})
    client = TestClient(create_app(
        deps=make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE]),
        store=JobStore(), max_revisions=1,
    ))

    job_id = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}).json()["id"]
    body = wait_for(client, job_id)

    writer_steps = [s for s in body["steps"] if s["agent"] == "Writer Agent"]
    assert writer_steps, "the writer must report at least one wave"

    branched = [s for s in writer_steps if len(s["parallel"]) > 1]
    assert branched, "no wave reported concurrent branches"
    assert set(branched[0]["parallel"]) == {"Financials", "Competition", "Risks"}


def test_every_step_carries_how_long_its_agent_took():
    writer, reviewer = build_models()
    client = TestClient(create_app(
        deps=make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE]),
        store=JobStore(), max_revisions=1,
    ))

    job_id = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}).json()["id"]
    body = wait_for(client, job_id)

    assert body["steps"], "a finished run must have steps"
    assert all("seconds" in s for s in body["steps"])
    assert all(s["seconds"] >= 0 for s in body["steps"])


def test_the_page_can_ask_whether_a_period_is_ambiguous(client):
    """Advisory endpoint: it warns, it never blocks."""
    warned = client.get("/api/period-check", params={"company": "Nvidia", "quarter": "Q1 2025"})
    assert warned.status_code == 200
    assert "fiscal year" in warned.json()["warning"]

    clear = client.get("/api/period-check", params={"company": "Shopify", "quarter": "Q1 2025"})
    assert clear.json()["warning"] == ""


def test_an_ambiguous_period_is_still_allowed_to_run(client):
    """The warning informs the user; it must not become a gate."""
    res = client.post("/api/reports", json={"company": "Nvidia", "quarter": "Q1 2025"})
    assert res.status_code == 202


def test_the_page_is_given_the_evidence_not_just_its_count(client):
    """Counting provenance is not showing it: which finding is unsourced?"""
    job_id = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}).json()["id"]
    body = wait_for(client, job_id)

    findings = body["findings"]
    assert len(findings) == 2, "the scripted research returns two findings"

    sourced = [f for f in findings if f["source_ids"]]
    unsourced = [f for f in findings if not f["source_ids"]]
    assert len(sourced) == 1 and len(unsourced) == 1
    assert "Headcount" in unsourced[0]["claim"], "the unsourced one is identifiable"
    assert body["provenance"] == {"sourced": 1, "total": 2, "unsourced": 1}


def test_sources_keep_the_indices_the_report_cites(client):
    """The report says [0]; sources[0] must be that source, so order is kept."""
    job_id = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}).json()["id"]
    body = wait_for(client, job_id)

    assert body["sources"], "a run with search results must expose them"
    assert body["sources"][0]["title"] == SOURCE.title
    assert body["sources"][0]["url"] == SOURCE.url

    cited = {i for f in body["findings"] for i in f["source_ids"]}
    assert all(i < len(body["sources"]) for i in cited), "every cited index resolves"


def test_steps_come_back_in_the_order_they_happened():
    """The page renders steps in the order this list gives them.

    It used to group them by agent instead, which put "pass 2, 7 sections
    revised" above the review that asked for the revision -- an effect shown
    before its cause, and it hid the revise cycle the page exists to show. The
    page now trusts this order, so the order is the contract.
    """
    writer, reviewer = build_models(reviews=[
        Review(approved=False, issues=[
            Issue(severity="major", section="Financial Performance",
                  problem="unsupported", fix="cite it"),
        ]),
        Review(approved=True),
    ])
    client = TestClient(create_app(
        deps=make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE]),
        store=JobStore(), max_revisions=2,
    ))

    job_id = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}).json()["id"]
    agents = [s["agent"] for s in wait_for(client, job_id)["steps"]]

    # A revision happened, so the Writer must appear both before and after a
    # Reviewer step. Grouping by agent makes that impossible to express.
    first_review = agents.index("Reviewer Agent")
    assert "Writer Agent" in agents[:first_review], "no drafting before the first review"
    assert "Writer Agent" in agents[first_review:], (
        f"the revision pass must follow the review that asked for it: {agents}"
    )
