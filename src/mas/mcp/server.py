"""MCP tools over the existing pipeline.

Nothing here re-implements the system. `Access` decides who may spend credits,
`JobStore` and `run_job` run the report, `gallery` serves the saved ones,
`period` validates the quarter, and `Job.as_dict()` already carries the steps,
timings, provenance, review verdict and per-agent cost. This layer adapts them
to MCP and adds the two things an agent caller needs that a person did not:

**A structured error instead of prose.** A model cannot act on "that didn't
work". Every failure returns `{error: {code, message, fix}}`, where `fix` says
what to change -- a bad period comes back with the corrected spelling, a
missing code says which argument to supply.

**A ceiling on spending.** A person clicks once; an agent can loop. `run_report`
refuses once `MAS_MCP_MAX_JOBS` reports are already in flight (default 2), so a
confused caller costs a bounded amount rather than the whole balance.

The SDK import lives inside `build_server`, not at module scope, so this module
imports with the extra absent and `test_every_module_imports` keeps passing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..period import InvalidPeriod, ambiguity_warning, validate
from ..web.access import Access
from ..web.gallery import index as gallery_index
from ..web.gallery import load as gallery_load
from ..web.jobs import JobStore, run_job
from .audit import AuditLog, new_request_id

log = logging.getLogger(__name__)

DEFAULT_MAX_JOBS = 2

#: Statuses a job can still leave. Anything else is finished, one way or another.
LIVE_STATUSES = ("queued", "running")


def error(code: str, message: str, fix: str) -> dict:
    """The one shape every failure takes.

    `fix` is the part that matters for a model caller: an error it cannot act
    on turns into a retry of the identical call.
    """
    return {"error": {"code": code, "message": message, "fix": fix}}


def max_jobs() -> int:
    """How many reports may be in flight at once."""
    try:
        return max(1, int(os.getenv("MAS_MCP_MAX_JOBS", DEFAULT_MAX_JOBS)))
    except ValueError:
        return DEFAULT_MAX_JOBS


def _evidence(job_dict: dict) -> list[dict]:
    """Findings with their sources attached, numbered as the report cites them.

    The indices are the `[n]` markers in the report text, so a caller can take
    a claim from the prose and look up what backs it. An unsourced finding is
    marked rather than omitted: the absence is the point, and dropping it would
    make a report look better sourced than it is.
    """
    sources = job_dict.get("sources") or []
    out = []
    for finding in job_dict.get("findings") or []:
        ids = [i for i in (finding.get("source_ids") or []) if 0 <= i < len(sources)]
        out.append(
            {
                "claim": finding.get("claim", ""),
                "topic": finding.get("topic", ""),
                "confidence": finding.get("confidence", ""),
                "sourced": bool(ids),
                "unsourced_warning": (
                    "" if ids else "Model recollection: no retrieved source backs this."
                ),
                "sources": [
                    {"index": i, "title": sources[i].get("title", ""),
                     "url": sources[i].get("url", "")}
                    for i in ids
                ],
            }
        )
    return out


def build_server(
    *,
    access: Access | None = None,
    store: JobStore | None = None,
    deps_factory=None,
    audit: AuditLog | None = None,
    max_revisions: int | None = None,
    runner=None,
):
    """Assemble the MCP server.

    Everything is injectable for the same reason `Deps` exists: a test supplies
    a scripted model and an in-memory store and never touches the network.
    `runner` replaces the background execution of a job, which is what keeps the
    tests from needing threads.
    """
    from mcp.server.mcpserver import MCPServer  # optional extra; see module docstring

    access = access or Access()
    store = store or JobStore()
    audit = audit or AuditLog()

    if deps_factory is None:
        def deps_factory():
            from ..config import load_settings
            from ..deps import Deps

            return Deps.from_settings(load_settings())

    if runner is None:
        def runner(job):
            import threading

            threading.Thread(
                target=run_job, args=(store, job, deps_factory(), max_revisions), daemon=True
            ).start()

    server = MCPServer(
        name="mas",
        instructions=(
            "Multi-agent market research reports. Four agents research, plan, draft "
            "and fact-check a report on a public company. Reports are produced by "
            "submit-and-poll: run_report returns a job_id immediately, then poll "
            "get_report_status. Saved reports are readable without an access code; "
            "running a new one spends API credits and may require one."
        ),
    )

    # ------------------------------------------------------------- gating

    def gate(tool: str, *, spends: bool, code: str | None, args: dict) -> tuple[dict | None, str]:
        """Authorise one call, record it, and return (error_or_None, request_id).

        Both outcomes are logged. A denial that left no trace would make the
        audit answer "what did it do" but not "what did it try", and a caller
        probing for an unlocked tool is exactly what is worth seeing.
        """
        request_id = new_request_id()
        permitted = access.may_run(code) if spends else access.may_view(code)
        if permitted:
            audit.record(tool, request_id=request_id, access_mode=access.mode,
                         allowed=True, outcome="ok", args=args)
            return None, request_id

        reason = (
            f"MAS_ACCESS={access.mode} requires an access code to "
            + ("run a report, which spends API credits." if spends else "read anything.")
        )
        audit.record(tool, request_id=request_id, access_mode=access.mode,
                     allowed=False, outcome="forbidden", args=args, reason=reason)
        return (
            error(
                "forbidden",
                reason,
                "Pass the server's access code as `access_code`. Saved reports are "
                "readable without one via list_gallery_reports unless the mode is 'locked'.",
            ),
            request_id,
        )

    # -------------------------------------------------------------- tools

    @server.tool()
    def run_report(
        company: str,
        quarter: str,
        access_code: str | None = None,
        focus: str | None = None,
        max_revisions: int | None = None,
    ) -> dict:
        """Start a market research report. Returns a job_id immediately.

        The run takes 25-40 seconds and is not awaited here: poll
        get_report_status with the returned job_id until status is "done" or
        "failed". Spends API credits, so it may require an access code.

        Args:
            company: A real, publicly reporting company, e.g. "Datadog".
                A fictional name produces a wholly invented report.
            quarter: Reporting period including a year, e.g. "Q1 2025",
                "FY2024", "H1 2025". The year is required.
            access_code: The server's code, when the access mode requires one.
            focus: Optional angle to emphasise, e.g. "AI product revenue".
            max_revisions: Reviewer-to-Writer loops before publishing.
                Defaults to the server's setting. More does not mean better.
        """
        args = {"company": company, "quarter": quarter, "focus": focus,
                "max_revisions": max_revisions, "access_code": access_code}
        denied, request_id = gate("run_report", spends=True, code=access_code, args=args)
        if denied:
            return denied

        if not (company or "").strip():
            return error("invalid_company", "No company given.",
                         "Pass a real, publicly reporting company such as 'Datadog'.")

        try:
            validate(quarter)
        except InvalidPeriod as exc:
            audit.record("run_report", request_id=request_id, access_mode=access.mode,
                         allowed=True, outcome="invalid_period", args=args, reason=str(exc))
            return error(
                "invalid_period", str(exc),
                f"Add a year, e.g. '{(quarter or 'Q1').strip()} 2025'.",
            )

        live = sum(1 for j in store.recent(limit=200) if j.status in LIVE_STATUSES)
        if live >= max_jobs():
            reason = f"{live} report(s) already running; the limit is {max_jobs()}."
            audit.record("run_report", request_id=request_id, access_mode=access.mode,
                         allowed=True, outcome="busy", args=args, reason=reason)
            return error(
                "too_many_jobs", reason,
                "Poll get_report_status until a running job finishes, then retry. "
                "Raise MAS_MCP_MAX_JOBS on the server to allow more at once.",
            )

        try:
            deps_factory()
        except Exception as exc:
            # Never echo the exception's text wholesale: a misconfiguration can
            # carry the key. The type is enough to act on.
            return error("model_unavailable", f"Cannot reach the model ({type(exc).__name__}).",
                         "Check OPENAI_API_KEY on the server. It is never returned here.")

        job = store.create(company.strip(), quarter.strip(), (focus or "").strip())
        runner(job)
        log.info("mcp queued job %s for %s %s", job.id, company, quarter)

        return {
            "job_id": job.id,
            "status": job.status,
            "company": job.company,
            "quarter": job.quarter,
            # Advisory, never blocking: the ambiguity is real and only the
            # caller can settle it, so it is said before the credits are spent.
            "period_warning": ambiguity_warning(company, quarter),
            "next": "Poll get_report_status with this job_id; a run takes 25-40s.",
        }

    @server.tool()
    def get_report_status(job_id: str, access_code: str | None = None) -> dict:
        """Progress and, once finished, the report and what it cost.

        While running, returns the agents that have finished and how long each
        took. When done, adds the report markdown, the provenance count, the
        review verdict, any unresolved issues, and per-agent token and dollar
        cost.

        Args:
            job_id: The id returned by run_report.
            access_code: Required only when the access mode is "locked".
        """
        args = {"job_id": job_id, "access_code": access_code}
        denied, request_id = gate("get_report_status", spends=False, code=access_code, args=args)
        if denied:
            return denied

        job = store.get(job_id)
        if job is None:
            return error("unknown_job", f"No job with id {job_id!r}.",
                         "Use the job_id returned by run_report. Jobs do not "
                         "survive a server restart.")

        d = job.as_dict()
        out = {
            "job_id": d["id"],
            "status": d["status"],
            "company": d["company"],
            "quarter": d["quarter"],
            "steps": d["steps"],
        }
        if d["status"] == "failed":
            out["error"] = d["error"]
            return out
        if d["status"] != "done":
            out["next"] = "Not finished; poll again."
            return out

        out.update(
            {
                "report_markdown": d["report"],
                "provenance": d["provenance"],
                "approved": d["approved"],
                "review_status": (
                    "approved" if d["approved"]
                    else "published with unresolved issues (the normal outcome)"
                ),
                "open_issues": d["open_issues"],
                "cost": d.get("cost") or {},
            }
        )
        audit.record("get_report_status", request_id=request_id, access_mode=access.mode,
                     allowed=True, outcome="done", args=args, job_id=d["id"],
                     cost=d.get("cost") or None, provenance=d.get("provenance"))
        return out

    @server.tool()
    def get_report_evidence(job_id: str, access_code: str | None = None) -> dict:
        """Every finding behind a report, with the sources that back it.

        Indices match the [n] citation markers in the report text, so a claim
        in the prose can be traced to what supports it. Findings with no
        retrieved source are marked rather than omitted.

        Args:
            job_id: The id returned by run_report.
            access_code: Required only when the access mode is "locked".
        """
        args = {"job_id": job_id, "access_code": access_code}
        denied, _ = gate("get_report_evidence", spends=False, code=access_code, args=args)
        if denied:
            return denied

        job = store.get(job_id)
        if job is None:
            return error("unknown_job", f"No job with id {job_id!r}.",
                         "Use the job_id returned by run_report.")

        d = job.as_dict()
        findings = _evidence(d)
        return {
            "job_id": d["id"],
            "status": d["status"],
            "provenance": d["provenance"],
            "findings": findings,
            "sources": d.get("sources") or [],
            "unsourced_count": sum(1 for f in findings if not f["sourced"]),
        }

    @server.tool()
    def list_gallery_reports(access_code: str | None = None) -> dict:
        """Saved reports that need no API credits to read.

        Each is a real past run kept whole, including its unresolved reviewer
        objections. Read one with the resource uri `gallery://<slug>`.

        Args:
            access_code: Required only when the access mode is "locked".
        """
        args = {"access_code": access_code}
        denied, _ = gate("list_gallery_reports", spends=False, code=access_code, args=args)
        if denied:
            return denied

        entries = gallery_load()
        return {
            "reports": [
                {**row, "uri": f"gallery://{row['slug']}"} for row in gallery_index(entries)
            ],
            "note": "Real runs of this pipeline, saved whole. Reading these spends nothing.",
        }

    @server.tool()
    def get_gallery_report(slug: str, access_code: str | None = None) -> dict:
        """One saved report in full, with its evidence and open issues.

        Args:
            slug: From list_gallery_reports, e.g. "shopify-q1-2025".
            access_code: Required only when the access mode is "locked".
        """
        args = {"slug": slug, "access_code": access_code}
        denied, _ = gate("get_gallery_report", spends=False, code=access_code, args=args)
        if denied:
            return denied

        entry = gallery_load().get((slug or "").strip().lower())
        if entry is None:
            return error("unknown_report", f"No saved report with slug {slug!r}.",
                         "Call list_gallery_reports for the available slugs.")
        return {
            "slug": slug,
            "company": entry.get("company", ""),
            "quarter": entry.get("quarter", ""),
            "report_markdown": entry.get("report", ""),
            "provenance": entry.get("provenance", {}),
            "approved": bool(entry.get("approved")),
            "open_issues": entry.get("open_issues", []),
            "findings": _evidence(entry),
            "steps": entry.get("steps", []),
        }

    # ---------------------------------------------------------- resources

    @server.resource(
        "gallery://{slug}",
        description="A saved report, as markdown. Costs nothing to read.",
        mime_type="text/markdown",
    )
    def gallery_resource(slug: str) -> str:
        entry = gallery_load().get((slug or "").strip().lower())
        if entry is None:
            return f"No saved report with slug {slug!r}."
        return entry.get("report", "")

    server.mas_access = access       # exposed for tests and the CLI banner
    server.mas_store = store
    server.mas_audit = audit
    return server
