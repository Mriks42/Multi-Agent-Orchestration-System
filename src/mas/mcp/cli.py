"""`mas-mcp`: serve the pipeline over MCP on stdio.

stdio is the transport an MCP client launches as a subprocess, so **stdout
belongs to the protocol**. Anything else printed there corrupts the stream and
the client sees a parse error rather than a message. Logging therefore goes to
stderr, and so does the startup banner.

Refuses to start in a gated mode with no code, exactly as `mas-serve` does --
the failure this prevents is a server that quietly lets anyone spend the key
because a variable was misspelled.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from ..web.access import Access, Misconfigured


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mas-mcp",
        description="Serve market research tools to an MCP client over stdio.",
    )
    parser.add_argument(
        "--access",
        choices=("gallery", "open", "locked"),
        default=None,
        help="Who may call what. Default: MAS_ACCESS, or 'gallery'.",
    )
    parser.add_argument(
        "--audit",
        default=None,
        help="Audit log path. Default: MAS_MCP_AUDIT, or mas-mcp-audit.jsonl.",
    )
    parser.add_argument(
        "--max-revisions", type=int, default=None,
        help="Reviewer -> Writer loops before publishing.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Log every tool call")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    load_dotenv()
    # stderr, never stdout: stdout carries the MCP protocol itself.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        access = Access(mode=args.access)
    except Misconfigured as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    try:
        from .audit import AuditLog
        from .server import build_server
    except ImportError as exc:
        print(
            f"The MCP server needs its optional extra: pip install -e \".[mcp]\" ({exc})",
            file=sys.stderr,
        )
        return 2

    server = build_server(
        access=access,
        audit=AuditLog(args.audit),
        max_revisions=args.max_revisions,
    )
    print(
        f"mas-mcp ready on stdio | access={access.mode} | "
        f"audit={server.mas_audit.path}",
        file=sys.stderr,
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
