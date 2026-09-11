"""Saved reports, so a visitor without a code still sees the real thing.

These are not marketing samples. Each one is a genuine run of this pipeline,
kept whole: the agent timings, the fan-out, the provenance count, the evidence,
and the reviewer's unresolved objections. A gallery that showed only the
flattering parts would be a worse advertisement than showing none -- the
unresolved blockers are the most honest thing on the page.

Entries are JSON files in `gallery/`, written from a finished job. They carry
no job id and no status: a saved report is not a running job, and the page
renders it through exactly the same code path as a live one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "gallery"


def _slug(entry: dict) -> str:
    return f"{entry.get('company', '')}-{entry.get('quarter', '')}".lower().replace(" ", "-")


def load(directory: Path | None = None) -> dict[str, dict]:
    """Every saved report, keyed by slug. A bad file is skipped, not fatal.

    A malformed entry must not take the whole page down: the gallery is what a
    visitor without a code sees, so it failing closed would leave them with
    nothing at all.
    """
    directory = directory or DEFAULT_DIR
    if not directory.is_dir():
        log.info("no gallery directory at %s", directory)
        return {}

    entries: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("skipping gallery entry %s: %s", path.name, exc)
            continue
        if not entry.get("report"):
            log.warning("skipping gallery entry %s: no report text", path.name)
            continue
        entry.setdefault("status", "done")
        entries[_slug(entry)] = entry

    log.info("loaded %d gallery report(s)", len(entries))
    return entries


def index(entries: dict[str, dict]) -> list[dict]:
    """The listing the page shows: enough to choose one, not the whole report."""
    return [
        {
            "slug": slug,
            "company": e.get("company", ""),
            "quarter": e.get("quarter", ""),
            "sourced": e.get("provenance", {}).get("sourced", 0),
            "total": e.get("provenance", {}).get("total", 0),
            "open_issues": len(e.get("open_issues", [])),
            "approved": bool(e.get("approved")),
        }
        for slug, e in sorted(entries.items())
    ]
