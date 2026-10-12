"""MCP server: the same pipeline, driven by an agent instead of a person.

Optional. The SDK is an extra (`pip install -e ".[mcp]"`), and it is imported
inside `build_server` rather than here, so every module in this package stays
importable without it -- the same arrangement `distributed/redis_broker.py`
uses for its client.
"""

from .audit import AuditLog, sanitise

__all__ = ["AuditLog", "sanitise"]
