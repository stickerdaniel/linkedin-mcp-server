"""HTTP liveness endpoint for platform probes (e.g. Railway health checks)."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fastmcp import FastMCP

HEALTH_PATH = "/health"


def register_health_route(mcp: FastMCP) -> None:
    """Register an unauthenticated liveness endpoint on the HTTP app.

    The route is outside MCP auth middleware so Railway and other probes can
    call it without a bearer token.
    """

    @mcp.custom_route(
        HEALTH_PATH,
        methods=["GET", "HEAD"],
        name="health",
        include_in_schema=False,
    )
    async def health_check(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})
