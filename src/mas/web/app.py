"""HTTP API over the report pipeline.

Submit-and-poll rather than a blocking request, because a report takes ~25
seconds and the four agents finishing one by one is the interesting part -- a
spinner would hide the whole architecture behind a blank wait.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fastapi import BackgroundTasks, Cookie, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..config import load_settings
from ..deps import Deps
from ..llm import MissingAPIKey
from ..period import InvalidPeriod, ambiguity_warning, validate
from . import gallery as gallery_store
from .access import COOKIE, Access
from .jobs import JobStore, run_job

log = logging.getLogger(__name__)

PAGE = Path(__file__).parent / "index.html"


class ReportRequest(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    quarter: str = Field(min_length=1, max_length=60)
    focus: str = Field(default="", max_length=200)


class Unlock(BaseModel):
    """Module level, not nested in `create_app`.

    This file uses `from __future__ import annotations`, so FastAPI resolves
    the body type by name against module globals. A class defined inside the
    factory is invisible there, and the route silently degrades to treating the
    body as a query parameter -- every request answering 422.
    """

    code: str = Field(min_length=1, max_length=200)


def create_app(deps: Deps | None = None, store: JobStore | None = None,
               max_revisions: int | None = None, access: Access | None = None,
               gallery: dict | None = None) -> FastAPI:
    """Build the app. `deps` and `store` are injectable so tests need no API key."""
    app = FastAPI(title="Multi-Agent Research", docs_url="/api/docs")
    app.state.store = store or JobStore()
    app.state.deps = deps
    app.state.max_revisions = max_revisions
    app.state.deps_lock = threading.Lock()
    # Default to open locally: a developer running mas-serve should not need to
    # invent an access code. Deployment sets MAS_ACCESS explicitly.
    app.state.access = access or Access(mode=os.getenv("MAS_ACCESS", "open"))
    app.state.gallery = gallery_store.load() if gallery is None else gallery

    def get_deps() -> Deps:
        """Build Deps once, on first use, so an absent API key fails per-request."""
        with app.state.deps_lock:
            if app.state.deps is None:
                app.state.deps = Deps.from_settings(load_settings())
            return app.state.deps

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        # Re-read per request so an edit shows up on refresh -- and say
        # no-store, or the browser serves its own copy and the re-read achieves
        # nothing. Without this a fixed page still rendered the old JavaScript
        # until a hard refresh, which cost an afternoon of believing a fix had
        # not worked. The page is a few KB and the demo is one instance.
        return HTMLResponse(
            PAGE.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/api/health")
    def health() -> dict:
        settings = load_settings()
        return {
            "ok": True,
            "model": settings.model,
            "reviewer_model": settings.reviewer_model,
            "jobs": len(app.state.store.recent(limit=1000)),
        }

    @app.get("/api/access")
    def access_state(mas_access: str | None = Cookie(default=None)) -> dict:
        """What this visitor may do, and what the page should therefore show."""
        state = app.state.access.describe(mas_access)
        state["gallery"] = gallery_store.index(app.state.gallery)
        return state

    @app.post("/api/access")
    def unlock(request: Unlock, response: Response) -> dict:
        """Exchange a correct code for a cookie. Wrong codes say so plainly."""
        if not app.state.access.check(request.code):
            raise HTTPException(status_code=403, detail="That code is not right.")
        # httponly: the code is not something page scripts ever need to read.
        response.set_cookie(
            COOKIE, request.code, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
        )
        return app.state.access.describe(request.code)

    @app.get("/api/gallery/{slug}")
    def gallery_entry(slug: str, mas_access: str | None = Cookie(default=None)) -> dict:
        if not app.state.access.may_view(mas_access):
            raise HTTPException(status_code=403, detail="An access code is required.")
        entry = app.state.gallery.get(slug)
        if entry is None:
            raise HTTPException(status_code=404, detail="No such saved report")
        return entry

    @app.get("/api/period-check")
    def period_check(company: str = "", quarter: str = "") -> dict:
        """Whether this company and period name two different quarters.

        Advisory only -- the run is never blocked on it. The ambiguity is real
        and the person asking is the only one who can settle it, so the page
        says so before the credits are spent rather than after.
        """
        return {"warning": ambiguity_warning(company, quarter)}

    @app.post("/api/reports", status_code=202)
    def submit(request: ReportRequest, background: BackgroundTasks,
               mas_access: str | None = Cookie(default=None)) -> dict:
        # The only route that spends money, so the only one that is gated.
        if not app.state.access.may_run(mas_access):
            raise HTTPException(
                status_code=403,
                detail="Running a new report needs an access code. "
                       "The saved reports below are real runs of this pipeline.",
            )

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
