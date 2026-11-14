"""Response models shared by the MCP tool and the REST endpoint.

One shape for both transports: anything an agent sees over MCP is byte-for-byte
the object curl returns. That keeps the two paths from drifting.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BackendStatus = Literal["ok", "error", "skipped"]


class Hit(BaseModel):
    title: str
    url: str
    snippet: str = ""
    engine: str


class BackendReport(BaseModel):
    name: str
    status: BackendStatus
    count: int = 0
    latency_ms: int = 0
    error_code: str | None = None
    detail: str | None = None


class SearchResponse(BaseModel):
    query: str
    lang: str
    count: int
    results: list[Hit] = Field(default_factory=list)
    backends: list[BackendReport] = Field(default_factory=list)
    partial: bool = False
    cached: bool = False
    error_code: str | None = None

    def failed(self) -> bool:
        # No backends at all is a failure too: an empty list must never be the
        # only answer the caller gets.
        return not self.backends or all(b.status != "ok" for b in self.backends)
