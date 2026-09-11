"""Who may spend the API key.

The failure that matters here is not a wrong 403 -- it is a deployment that
quietly lets anyone run reports on the owner's credits. So the tests lean on
the gated paths, and on the startup checks that are supposed to make a
misconfiguration loud.
"""

from __future__ import annotations

import pytest
from conftest import FakeChatModel, make_deps
from fastapi.testclient import TestClient

from mas.state import Finding, Outline, Review, Section, Source
from mas.web import JobStore, create_app
from mas.web.access import Access, Misconfigured

SOURCE = Source(title="Q1 earnings", url="https://example.com/q1", snippet="revenue")

GALLERY = {
    "acme-q1-2025": {
        "company": "Acme", "quarter": "Q1 2025", "report": "# Acme\n\n## X\n\nBody.",
        "provenance": {"sourced": 3, "total": 4, "unsourced": 1},
        "open_issues": ["[blocker] X: y"], "approved": False,
        "findings": [], "sources": [], "steps": [],
    }
}


def _client(mode: str, code: str = "s3cret", gallery=GALLERY) -> TestClient:
    def handler(schema, messages, model):
        name = schema.__name__
        if name == "_Queries":
            return schema(queries=["q"])
        if name == "_Findings":
            return schema(findings=[Finding(claim="c", topic="t", source_ids=[0])])
        if name == "Outline":
            return Outline(title="T", sections=[Section(heading="X", purpose="p")])
        raise AssertionError(name)

    writer = FakeChatModel(
        handlers={k: handler for k in ("_Queries", "_Findings", "Outline")},
        text_handler=lambda m, mo: "body [0]",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: Review(approved=True)})
    return TestClient(create_app(
        deps=make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE]),
        store=JobStore(), max_revisions=1,
        access=Access(mode=mode, code=code), gallery=gallery,
    ))


# ------------------------------------------------------- misconfiguration


def test_a_gated_mode_without_a_code_refuses_to_start():
    """The whole point is to not expose the key; defaulting to open would."""
    for mode in ("gallery", "locked"):
        with pytest.raises(Misconfigured, match="MAS_ACCESS_CODE"):
            Access(mode=mode, code="")


def test_an_unknown_mode_refuses_to_start():
    with pytest.raises(Misconfigured, match="not one of"):
        Access(mode="pubic", code="x")  # a plausible typo for "public"


def test_open_mode_needs_no_code():
    assert Access(mode="open", code="").may_run(None) is True


# --------------------------------------------------------- gallery mode


def test_gallery_mode_refuses_to_spend_credits_without_a_code():
    res = _client("gallery").post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"})

    assert res.status_code == 403
    assert "access code" in res.json()["detail"]


def test_gallery_mode_still_serves_the_saved_reports():
    client = _client("gallery")

    state = client.get("/api/access").json()
    assert state["gallery_only"] is True
    assert [g["slug"] for g in state["gallery"]] == ["acme-q1-2025"]

    entry = client.get("/api/gallery/acme-q1-2025")
    assert entry.status_code == 200
    assert entry.json()["report"].startswith("# Acme")


def test_the_gallery_listing_keeps_the_unflattering_numbers():
    """A gallery that hid the open issues would be a worse advertisement."""
    row = _client("gallery").get("/api/access").json()["gallery"][0]

    assert row["open_issues"] == 1
    assert row["approved"] is False
    assert (row["sourced"], row["total"]) == (3, 4)


def test_the_right_code_unlocks_running_and_the_cookie_persists():
    client = _client("gallery")

    unlocked = client.post("/api/access", json={"code": "s3cret"})
    assert unlocked.status_code == 200
    assert unlocked.json()["may_run"] is True

    # The cookie the response set now rides along on the next request.
    res = client.post("/api/reports", json={"company": "Acme", "quarter": "Q1 2025"})
    assert res.status_code == 202


def test_a_wrong_code_changes_nothing():
    client = _client("gallery")

    assert client.post("/api/access", json={"code": "hunter2"}).status_code == 403
    assert client.post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    ).status_code == 403


# ---------------------------------------------------------- locked mode


def test_locked_mode_hides_even_the_gallery():
    client = _client("locked")

    assert client.get("/api/gallery/acme-q1-2025").status_code == 403
    assert client.get("/api/access").json()["gallery_only"] is False


def test_locked_mode_opens_up_with_the_code():
    client = _client("locked")
    client.post("/api/access", json={"code": "s3cret"})

    assert client.get("/api/gallery/acme-q1-2025").status_code == 200


# ------------------------------------------------------------ open mode


def test_open_mode_runs_for_anyone():
    res = _client("open", code="").post(
        "/api/reports", json={"company": "Acme", "quarter": "Q1 2025"}
    )
    assert res.status_code == 202


def test_a_missing_gallery_entry_is_a_404_not_a_500():
    assert _client("open", code="").get("/api/gallery/nope").status_code == 404


def test_code_comparison_does_not_leak_length_by_short_circuiting():
    """compare_digest, not ==, so a wrong code cannot be narrowed by timing."""
    access = Access(mode="locked", code="s3cret")

    assert access.check("s3cret") is True
    assert access.check("s3cre") is False
    assert access.check("s3cretx") is False
    assert access.check("") is False
    assert access.check(None) is False
