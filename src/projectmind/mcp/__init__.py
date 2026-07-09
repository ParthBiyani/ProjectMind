"""MCP transport. A thin adapter over `projectmind.service.MemoryService`."""

from projectmind.mcp.server import SERVER_NAME, build_server, main

__all__ = ["SERVER_NAME", "build_server", "main"]
