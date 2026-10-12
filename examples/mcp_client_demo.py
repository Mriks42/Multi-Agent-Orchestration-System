"""Drive the MCP server as a client would, without spending anything.

Launches `mas-mcp` as a subprocess over stdio -- the same transport an MCP
client uses -- lists the tools, reads a saved report, and prints the audit
trail it just produced.

Runs in gallery mode, so every call here is free: the gallery is readable
without a code and `run_report`, the one tool that costs money, is never
called. The denial it does provoke is deliberate, to show that a refusal is
structured and that it reaches the log.

    python examples/mcp_client_demo.py

Needs the extra: pip install -e ".[mcp]"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - the extra is optional
    sys.exit("This demo needs the MCP extra: pip install -e \".[mcp]\"")

AUDIT = Path(tempfile.gettempdir()) / "mas-mcp-demo-audit.jsonl"


def payload(result) -> dict:
    """A tool's JSON, out of the content block the protocol wraps it in."""
    return json.loads(result.content[0].text)


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def main() -> int:
    AUDIT.unlink(missing_ok=True)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mas.mcp.cli"],
        env={
            **os.environ,
            "MAS_ACCESS": "gallery",
            "MAS_ACCESS_CODE": "demo-code",
            "MAS_MCP_AUDIT": str(AUDIT),
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            rule("Tools this server exposes")
            for tool in (await session.list_tools()).tools:
                first_line = (tool.description or "").strip().splitlines()[0]
                print(f"  {tool.name:<22} {first_line}")

            rule("Saved reports (no access code, no credits)")
            listing = payload(await session.call_tool("list_gallery_reports", {}))
            for row in listing["reports"]:
                print(
                    f"  {row['company']:<12} {row['quarter']:<9} "
                    f"{row['sourced']}/{row['total']} sourced · "
                    f"{row['open_issues']} open issue(s) · {row['uri']}"
                )

            slug = listing["reports"][0]["slug"]
            rule(f"Reading {slug}")
            report = payload(await session.call_tool("get_gallery_report", {"slug": slug}))
            print(f"  provenance : {report['provenance']}")
            print(f"  approved   : {report['approved']}")
            unsourced = [f for f in report["findings"] if not f["sourced"]]
            print(f"  findings   : {len(report['findings'])} ({len(unsourced)} unsourced)")
            body = report["report_markdown"].strip().splitlines()
            print(f"  first line : {body[0] if body else '(empty)'}")

            rule("Running a new report needs a code (this is the refusal)")
            denied = payload(
                await session.call_tool(
                    "run_report", {"company": "Datadog", "quarter": "Q1 2025"}
                )
            )
            print(f"  code    : {denied['error']['code']}")
            print(f"  message : {denied['error']['message']}")
            print(f"  fix     : {denied['error']['fix']}")

    rule("Audit trail")
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        print(
            f"  {r['ts'][11:19]}  {r['tool']:<22} "
            f"{'allowed' if r['allowed'] else 'DENIED ':<8} {r['outcome']}"
        )
    print(f"\n  (full log: {AUDIT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
