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
