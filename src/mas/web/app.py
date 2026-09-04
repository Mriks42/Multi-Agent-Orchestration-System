"""HTTP API over the report pipeline.

Submit-and-poll rather than a blocking request, because a report takes ~25
seconds and the four agents finishing one by one is the interesting part -- a
spinner would hide the whole architecture behind a blank wait.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..config import load_settings
from ..deps import Deps
from ..llm import MissingAPIKey
from ..period import InvalidPeriod, validate
from .jobs import JobStore, run_job

log = logging.getLogger(__name__)

PAGE = Path(__file__).parent / "index.html"


class ReportRequest(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    quarter: str = Field(min_length=1, max_length=60)
    focus: str = Field(default="", max_length=200)


def create_app(deps: Deps | None = None, store: JobStore | None = None,
               max_revisions: int | None = None) -> FastAPI:
    """Build the app. `deps` and `store` are injectable so tests need no API key."""
    app = FastAPI(title="Multi-Agent Research", docs_url="/api/docs")
    app.state.store = store or JobStore()
    app.state.deps = deps
    app.state.max_revisions = max_revisions
    app.state.deps_lock = threading.Lock()

    def get_deps() -> Deps:
        """Build Deps once, on first use, so an absent API key fails per-request."""
        with app.state.deps_lock:
            if app.state.deps is None:
                app.state.deps = Deps.from_settings(load_settings())
            return app.state.deps

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict:
        settings = load_settings()
        return {
            "ok": True,
            "model": settings.model,
            "reviewer_model": settings.reviewer_model,
            "jobs": len(app.state.store.recent(limit=1000)),
        }

    @app.post("/api/reports", status_code=202)
    def submit(request: ReportRequest, background: BackgroundTasks) -> dict:
        try:
            validate(request.quarter)
        except InvalidPeriod as exc:
            # 422, not 500: the request is answerable, just not as written.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            deps = get_deps()
        except MissingAPIKey as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        job = app.state.store.create(request.company, request.quarter, request.focus)
        background.add_task(run_job, app.state.store, job, deps, app.state.max_revisions)
        log.info("queued job %s for %s %s", job.id, request.company, request.quarter)
        return {"id": job.id, "status": job.status}

    @app.get("/api/reports/{job_id}")
    def poll(job_id: str) -> dict:
        job = app.state.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        return job.as_dict()

    @app.get("/api/reports")
    def recent(limit: int = 20) -> dict:
        return {
            "reports": [
                {
                    "id": j.id, "company": j.company, "quarter": j.quarter,
                    "status": j.status,
                }
                for j in app.state.store.recent(limit=min(limit, 100))
            ]
        }

    return app
