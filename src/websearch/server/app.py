"""HTTP surface: MCP at /mcp, plain REST at /search, /healthz.

MCP and REST share one service instance and one response model, so an agent and
a curl command see the same data.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..config import Config
from ..core import SearchService
from ..models import SearchResponse
from ..service import build_service
from .auth import BearerAuth

INSTRUCTIONS = (
    "Search the web. Results always carry a per-backend report: when `partial` is true, "
    "read `backends[].error_code` (captcha, blocked, timeout, ...) instead of assuming the "
    "query had no matches."
)


def _int(value, default: int | None = None) -> int | None:
    try:
        return int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        return default


def build_app(config: Config | None = None, service: SearchService | None = None) -> Starlette:
    config = config or Config.load()
    if config.server.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            f"server.host is {config.server.host!r}: this service is localhost-only"
        )
    service = service or build_service(config)

    server = MCPServer(
        "websearch",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    @server.tool(structured_output=True)
    async def search(
        query: str, count: int | None = None, lang: str | None = None
    ) -> SearchResponse:
        """Web search through DuckDuckGo and Mwmbl.

        Args:
            query: Search terms, English or Persian.
            count: Number of merged results (default from config, capped by max_count).
            lang: "auto" (default, detects Persian script), "fa", "en", or a DDG
                regional code such as "de-de".
        """
        return await service.search(query, count, lang)

    async def rest_search(request: Request) -> JSONResponse:
        if request.method == "GET":
            query = request.query_params.get("q", "")
            count = _int(request.query_params.get("count"))
            raw_lang = request.query_params.get("lang")
        else:
            try:
                payload = await request.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            raw_query = payload.get("q") or payload.get("query") or ""
            query = raw_query if isinstance(raw_query, str) else ""
            count = _int(payload.get("count"))
            raw_lang = payload.get("lang")
        # REST has no schema validation; reject wrong types instead of letting
        # something like {"lang": ["fa"]} reach .lower() and become a 500.
        if raw_lang is not None and not isinstance(raw_lang, str):
            return JSONResponse(
                {"error": "lang must be a string", "error_code": "bad_request"}, status_code=400
            )
        lang = raw_lang.strip() if isinstance(raw_lang, str) and raw_lang.strip() else None
        if not query.strip():
            return JSONResponse(
                {"error": "query is required", "error_code": "bad_request"}, status_code=400
            )
        try:
            response = await service.search(query, count, lang)
        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc), "error_code": "bad_request"}, status_code=400
            )
        return JSONResponse(
            response.model_dump(), status_code=502 if response.failed() else 200
        )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(service.health())

    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)
    app.routes.append(Route("/search", rest_search, methods=["GET", "POST"]))
    app.routes.append(Route("/healthz", health, methods=["GET"]))
    app.add_middleware(BearerAuth, token=config.ensure_token())

    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with inner_lifespan(application):
            yield
        await service.close()

    app.router.lifespan_context = lifespan
    return app
