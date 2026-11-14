"""Bearer-token gate for every route except /healthz.

The service binds to loopback, but other local users (and any process that finds
the port) would otherwise get free access to a rate-limited upstream.
"""

from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

EXEMPT_PATHS = {"/healthz"}


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token
        self._token_bytes = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode(), self._token_bytes
        ):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "unauthorized", "error_code": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
