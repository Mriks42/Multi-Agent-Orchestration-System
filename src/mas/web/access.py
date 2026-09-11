"""Who may spend the API key, and what everyone else gets instead.

A public URL is what makes this project visible to someone who will not clone
it. It is also an open invitation to spend the owner's OpenAI credits at
roughly 25 calls a report, from anyone who finds the link.

The answer here is not to lock the door. A locked page proves nothing to a
visitor, and a link that shows a stranger nothing is worse than no link. So the
default is a **read-only gallery**: anyone can read real reports this pipeline
produced, complete with their evidence and their unresolved reviewer issues.
Running a *new* company -- the part that costs money -- needs an access code.

Three modes, chosen by environment variable so the decision is a deploy-time
setting rather than a code change:

    MAS_ACCESS=gallery   (default) browse saved reports; a code unlocks running
    MAS_ACCESS=open      anyone may run one; nothing is gated
    MAS_ACCESS=locked    a code is required even to look

`gallery` and `locked` need MAS_ACCESS_CODE set. If it is missing the app
refuses to start rather than defaulting to open -- a deployment that silently
exposed the key because a variable was misspelled is exactly the failure this
exists to prevent.
"""

from __future__ import annotations

import hmac
import logging
import os

log = logging.getLogger(__name__)

MODES = ("gallery", "open", "locked")
COOKIE = "mas_access"


class Misconfigured(RuntimeError):
    """Raised at startup when a gated mode has no code to check against."""


class Access:
    """The access policy for one running app."""

    def __init__(self, mode: str | None = None, code: str | None = None):
        self.mode = (mode or os.getenv("MAS_ACCESS") or "gallery").strip().lower()
        self.code = (code if code is not None else os.getenv("MAS_ACCESS_CODE", "")).strip()

        if self.mode not in MODES:
            raise Misconfigured(
                f"MAS_ACCESS={self.mode!r} is not one of {', '.join(MODES)}."
            )
        if self.mode in ("gallery", "locked") and not self.code:
            raise Misconfigured(
                f"MAS_ACCESS={self.mode} needs MAS_ACCESS_CODE set, or anyone "
                f"who finds the URL can spend your API credits. Set a code, or "
                f"choose MAS_ACCESS=open deliberately."
            )
        log.info("access mode: %s", self.mode)

    def check(self, supplied: str | None) -> bool:
        """Is this the right code? Constant-time, so it cannot be guessed by timing."""
        if not self.code:
            return False
        return hmac.compare_digest((supplied or "").strip(), self.code)

    def may_run(self, supplied: str | None) -> bool:
        """May this visitor start a new report, which spends credits?"""
        return self.mode == "open" or self.check(supplied)

    def may_view(self, supplied: str | None) -> bool:
        """May this visitor see anything at all?"""
        return self.mode != "locked" or self.check(supplied)

    def describe(self, supplied: str | None) -> dict:
        """What the page needs to know to render itself."""
        unlocked = self.check(supplied)
        return {
            "mode": self.mode,
            "unlocked": unlocked,
            "may_run": self.may_run(supplied),
            "gallery_only": self.mode == "gallery" and not unlocked,
        }
