"""The MCP layer: who may call what, what a failure says, and what gets logged.

Everything here runs in-process against the scripted model from conftest. No
subprocess, no network, no API key, no credits. The server exposes `call_tool`
and `list_tools` directly, so the tools are driven exactly as a client drives
them without standing up a transport.

The SDK is an optional extra, so the whole module skips when it is absent --
`pytest` has to stay green on a default install.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp", reason="the MCP server is an optional extra: pip install -e '.[mcp]'")

from conftest import FakeChatModel, make_deps  # noqa: E402

from mas.mcp.audit import AuditLog, sanitise  # noqa: E402
from mas.mcp.server import build_server  # noqa: E402
from mas.state import Finding, Outline, Review, Section, Source  # noqa: E402
from mas.web.access import Access, Misconfigured  # noqa: E402
from mas.web.jobs import JobStore, run_job  # noqa: E402

CODE = "let-me-in"
SOURCE = Source(title="Q1 earnings", url="https://example.com/q1", snippet="revenue")


# ------------------------------------------------------------------- helpers


def call(server, tool: str, **args) -> dict:
    """Invoke one tool and return its payload, as a client would receive it."""
    result = asyncio.run(server.call_tool(tool, args))
    return json.loads(result.content[0].text)


def build_models():
    """A scripted model with one sourced finding and one that is not."""
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
                Section(heading="Financials", purpose="numbers"),
            ])
        raise AssertionError(name)

    writer = FakeChatModel(
        handlers={k: handler for k in ("_Queries", "_Findings", "Outline")},
        text_handler=lambda messages, m: "section body [0]",
    )
    reviewer = FakeChatModel(handlers={"Review": lambda s, m, mo: Review(approved=True)})
    return writer, reviewer


def make_server(tmp_path, mode="open", code=CODE, store=None, runner=None, max_jobs=None):
    store = store if store is not None else JobStore()
    calls: list = []

    def record_only(job):
        calls.append(job)

    server = build_server(
        access=Access(mode=mode, code=code),
        store=store,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        deps_factory=lambda: object(),
        runner=runner or record_only,
    )
    server.started = calls
    return server


def finished_job(tmp_path, store=None):
    """A real finished job, produced by the real graph against fakes."""
    store = store or JobStore()
    writer, reviewer = build_models()
    deps = make_deps(writer, reviewer, search=lambda q, n=5: [SOURCE])
    job = store.create("Acme", "Q1 2025")
    run_job(store, job, deps, 1)  # synchronous: no thread, no polling
    return store, job


# ------------------------------------------------------------- the tool surface


def test_every_tool_is_exposed_with_a_description(tmp_path):
    """A model picks a tool from its description, so an empty one is a bug."""
    server = make_server(tmp_path)
    tools = asyncio.run(server.list_tools())

    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "run_report", "get_report_status", "get_report_evidence",
        "list_gallery_reports", "get_gallery_report",
    }
    for name, tool in by_name.items():
        assert tool.description and len(tool.description) > 40, f"{name} needs a real description"
        assert tool.input_schema.get("properties"), f"{name} has no typed parameters"

    # The one that spends money must say so where a caller will read it.
    assert "credit" in by_name["run_report"].description.lower()


# -------------------------------------------------------------- access control


def test_gallery_mode_reads_without_a_code_but_will_not_run(tmp_path):
    server = make_server(tmp_path, mode="gallery")

    assert "error" not in call(server, "list_gallery_reports")
    denied = call(server, "run_report", company="Acme", quarter="Q1 2025")
    assert denied["error"]["code"] == "forbidden"
    assert server.started == [], "a denied call must not start a job"


def test_gallery_mode_runs_with_the_right_code(tmp_path):
    server = make_server(tmp_path, mode="gallery")
    out = call(server, "run_report", company="Acme", quarter="Q1 2025", access_code=CODE)
    assert "error" not in out and out["job_id"]


def test_locked_mode_gates_even_reading(tmp_path):
    server = make_server(tmp_path, mode="locked")

    assert call(server, "list_gallery_reports")["error"]["code"] == "forbidden"
    assert "error" not in call(server, "list_gallery_reports", access_code=CODE)


def test_open_mode_needs_no_code_at_all(tmp_path):
    server = make_server(tmp_path, mode="open", code="")
    assert "error" not in call(server, "list_gallery_reports")
    assert "error" not in call(server, "run_report", company="Acme", quarter="Q1 2025")


def test_a_wrong_code_is_refused(tmp_path):
    server = make_server(tmp_path, mode="gallery")
    out = call(server, "run_report", company="Acme", quarter="Q1 2025", access_code="guess")
    assert out["error"]["code"] == "forbidden"


@pytest.mark.parametrize("mode", ["gallery", "locked"])
def test_a_gated_mode_refuses_to_start_without_a_code(mode):
    """Fail closed, exactly as the web app does.

    A server that fell back to open because a variable was misspelled would
    hand its API key to anyone who connected.
    """
    with pytest.raises(Misconfigured):
        Access(mode=mode, code="")


# ------------------------------------------------------------------ the period


def test_a_period_without_a_year_is_refused_with_the_fix_in_it(tmp_path):
    server = make_server(tmp_path)
    out = call(server, "run_report", company="Acme", quarter="Q1")

    assert out["error"]["code"] == "invalid_period"
    assert "2025" in out["error"]["fix"], "the fix must show the corrected spelling"
    assert server.started == []


def test_an_ambiguous_period_warns_but_still_runs(tmp_path):
    """The NVIDIA case: advisory, never blocking.

    Only the caller can settle which quarter they meant, so the warning is
    returned beside the job id rather than used to refuse the run.
    """
    server = make_server(tmp_path)
    out = call(server, "run_report", company="NVIDIA", quarter="Q1 2025")

    assert out["job_id"], "an ambiguous period must not block the run"
    assert "fiscal" in out["period_warning"].lower()
    assert "FY2025" in out["period_warning"]


def test_an_unambiguous_period_carries_no_warning(tmp_path):
    server = make_server(tmp_path)
    assert call(server, "run_report", company="Shopify", quarter="Q1 2025")["period_warning"] == ""


def test_an_empty_company_is_refused(tmp_path):
    server = make_server(tmp_path)
    assert call(server, "run_report", company="  ", quarter="Q1 2025")["error"]["code"] == (
        "invalid_company"
    )


# --------------------------------------------------------------- submit & poll


def test_run_report_returns_a_job_id_without_waiting_for_the_run(tmp_path):
    """The whole point of submit-and-poll: a 25-40s run must not block a call."""
    server = make_server(tmp_path)
    out = call(server, "run_report", company="Acme", quarter="Q1 2025")

    assert out["job_id"] and out["status"] == "queued"
    assert "poll" in out["next"].lower()
    assert len(server.started) == 1, "the run was handed off exactly once"


def test_status_of_an_unfinished_job_says_so(tmp_path):
    server = make_server(tmp_path)
    job_id = call(server, "run_report", company="Acme", quarter="Q1 2025")["job_id"]

    out = call(server, "get_report_status", job_id=job_id)
    assert out["status"] == "queued"
    assert "report_markdown" not in out, "nothing to report until it finishes"


def test_status_of_a_finished_job_carries_the_report_cost_and_verdict(tmp_path):
    store, job = finished_job(tmp_path)
    server = make_server(tmp_path, store=store)

    out = call(server, "get_report_status", job_id=job.id)
    assert out["status"] == "done"
    assert "# Acme — Q1 2025" in out["report_markdown"]
    assert out["provenance"] == {"sourced": 1, "total": 2, "unsourced": 1}
    assert out["review_status"]
    assert out["cost"]["calls"] > 0, "per-agent cost must reach the caller"
    assert {r["agent"] for r in out["cost"]["agents"]} >= {"Research Agent", "Writer Agent"}
    assert out["steps"], "per-agent progress and timings must be included"


def test_an_unknown_job_id_is_a_structured_error(tmp_path):
    server = make_server(tmp_path)
    out = call(server, "get_report_status", job_id="nope")
    assert out["error"]["code"] == "unknown_job"
    assert out["error"]["fix"]


# -------------------------------------------------------------------- evidence


def test_evidence_marks_the_unsourced_finding_rather_than_hiding_it(tmp_path):
    """The absence is the point: dropping it would flatter the report."""
    store, job = finished_job(tmp_path)
    server = make_server(tmp_path, store=store)

    out = call(server, "get_report_evidence", job_id=job.id)
    claims = {f["claim"]: f for f in out["findings"]}

    assert claims["Revenue was $2.1B"]["sourced"] is True
    assert claims["Revenue was $2.1B"]["sources"][0]["url"] == SOURCE.url

    unsourced = claims["Headcount near 4000"]
    assert unsourced["sourced"] is False
    assert unsourced["sources"] == []
    assert "recollection" in unsourced["unsourced_warning"].lower()
    assert out["unsourced_count"] == 1


def test_evidence_indices_match_the_citation_markers(tmp_path):
    """A [0] in the prose has to resolve to the source listed at index 0."""
    store, job = finished_job(tmp_path)
    server = make_server(tmp_path, store=store)

    out = call(server, "get_report_evidence", job_id=job.id)
    assert out["sources"][0]["title"] == SOURCE.title
    assert out["findings"][0]["sources"][0]["index"] == 0


# --------------------------------------------------------------------- gallery


def test_the_gallery_lists_real_saved_reports(tmp_path):
    server = make_server(tmp_path)
    out = call(server, "list_gallery_reports")

    assert out["reports"], "the committed gallery should not be empty"
    first = out["reports"][0]
    assert first["uri"].startswith("gallery://")
    assert {"slug", "company", "quarter", "sourced", "total"} <= set(first)


def test_a_saved_report_can_be_read_in_full(tmp_path):
    server = make_server(tmp_path)
    slug = call(server, "list_gallery_reports")["reports"][0]["slug"]

    out = call(server, "get_gallery_report", slug=slug)
    assert out["report_markdown"]
    assert "findings" in out and "provenance" in out


def test_an_unknown_slug_says_how_to_find_a_real_one(tmp_path):
    server = make_server(tmp_path)
    out = call(server, "get_gallery_report", slug="no-such-report")
    assert out["error"]["code"] == "unknown_report"
    assert "list_gallery_reports" in out["error"]["fix"]


# ------------------------------------------------------------ spending ceiling


def test_the_concurrency_guard_stops_an_agent_spending_without_limit(tmp_path, monkeypatch):
    """A person clicks once; a model can loop."""
    monkeypatch.setenv("MAS_MCP_MAX_JOBS", "2")
    server = make_server(tmp_path)

    assert "error" not in call(server, "run_report", company="A", quarter="Q1 2025")
    assert "error" not in call(server, "run_report", company="B", quarter="Q1 2025")

    third = call(server, "run_report", company="C", quarter="Q1 2025")
    assert third["error"]["code"] == "too_many_jobs"
    assert "MAS_MCP_MAX_JOBS" in third["error"]["fix"]
    assert len(server.started) == 2, "the third run must never have been started"


def test_a_finished_job_frees_a_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("MAS_MCP_MAX_JOBS", "1")
    store, _ = finished_job(tmp_path)          # one job, already done
    server = make_server(tmp_path, store=store)

    assert "error" not in call(server, "run_report", company="Next", quarter="Q1 2025")


# ----------------------------------------------------------------- audit trail


def test_the_audit_log_masks_the_access_code(tmp_path):
    server = make_server(tmp_path, mode="gallery")
    call(server, "run_report", company="Acme", quarter="Q1 2025", access_code=CODE)
    server.mas_audit.flush()

    records = server.mas_audit.tail()
    assert records, "a successful call must be recorded"
    text = json.dumps(records)
    assert CODE not in text, "the access code reached the audit log"
    assert "***" in text


def test_denied_attempts_are_logged_too(tmp_path):
    """A success-only log answers what it did, never what it tried."""
    server = make_server(tmp_path, mode="gallery")
    call(server, "run_report", company="Acme", quarter="Q1 2025", access_code="wrong")
    server.mas_audit.flush()

    denied = [r for r in server.mas_audit.tail() if not r["allowed"]]
    assert denied, "a refused call left no trace"
    assert denied[0]["tool"] == "run_report"
    assert denied[0]["outcome"] == "forbidden"
    assert denied[0]["args"]["access_code"] == "***"
    assert denied[0]["reason"]


def test_every_record_carries_who_what_and_when(tmp_path):
    server = make_server(tmp_path, mode="gallery")
    call(server, "list_gallery_reports")
    server.mas_audit.flush()

    record = server.mas_audit.tail()[-1]
    assert {"ts", "request_id", "tool", "args", "access_mode", "allowed", "outcome"} <= set(record)
    assert record["access_mode"] == "gallery"
    assert record["ts"].startswith("20")


def test_a_finished_run_logs_what_it_cost(tmp_path):
    store, job = finished_job(tmp_path)
    server = make_server(tmp_path, store=store)
    call(server, "get_report_status", job_id=job.id)
    server.mas_audit.flush()

    costed = [r for r in server.mas_audit.tail() if r.get("cost")]
    assert costed, "a finished run's cost must reach the audit trail"
    assert costed[-1]["provenance"]["total"] == 2


def test_sanitise_keeps_whether_a_code_was_supplied():
    """"called with no code" and "called with a bad code" are different events."""
    assert sanitise({"access_code": "secret"})["access_code"] == "***"
    assert sanitise({"access_code": ""})["access_code"] is None
    assert sanitise({"company": "Acme"}) == {"company": "Acme"}


def test_the_audit_log_never_blocks_on_a_full_queue(tmp_path):
    """Losing a record beats stalling a tool call; the loss is counted."""
    audit = AuditLog(tmp_path / "small.jsonl", max_pending=1)
    for _ in range(200):
        audit.record("t", request_id="r", access_mode="open", allowed=True, outcome="ok")
    assert audit.dropped >= 0  # never raised, never hung


# ------------------------------------------------------------------- secrecy


def test_no_response_ever_contains_a_secret(tmp_path, monkeypatch):
    """The key and the code must not leak through any tool, including errors."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-never-appear")
    store, job = finished_job(tmp_path)
    server = make_server(tmp_path, mode="gallery", store=store)

    responses = [
        call(server, "list_gallery_reports"),
        call(server, "run_report", company="Acme", quarter="Q1 2025"),          # denied
        call(server, "run_report", company="Acme", quarter="Q1", access_code=CODE),  # bad period
        call(server, "get_report_status", job_id=job.id),
        call(server, "get_report_evidence", job_id=job.id),
        call(server, "get_report_status", job_id="nope"),
    ]
    blob = json.dumps(responses)
    assert "sk-must-never-appear" not in blob
    assert CODE not in blob


def test_a_model_that_cannot_be_built_does_not_leak_the_reason(tmp_path):
    """A misconfiguration message can carry the key, so only the type is returned."""
    def explode():
        raise RuntimeError("bad key sk-leaked-here")

    server = build_server(
        access=Access(mode="open"), store=JobStore(),
        audit=AuditLog(tmp_path / "a.jsonl"),
        deps_factory=explode, runner=lambda job: None,
    )
    out = call(server, "run_report", company="Acme", quarter="Q1 2025")
    assert out["error"]["code"] == "model_unavailable"
    assert "sk-leaked-here" not in json.dumps(out)
    assert "RuntimeError" in out["error"]["message"]
