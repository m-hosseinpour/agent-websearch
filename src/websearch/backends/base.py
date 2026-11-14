"""Backend contract and discovery.

A backend is a plain object: a name, a ``search`` coroutine, and nothing else.
It never opens sockets itself — it receives ``fetch`` from the egress layer so
route policy (direct vs tunnel, retries, learning) stays in one place.

Adding one:
  1. write a class with ``name`` and ``search(...)``;
  2. register it in ``[project.entry-points."websearch.backends"]``;
  3. nothing in core changes.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx

from ..models import Hit

# (status, body, headers) -> error code when the body is a block page, else None.
BlockDetector = Callable[[int, bytes, "httpx.Headers"], str | None]


class BackendError(Exception):
    """A failure with a code the caller can branch on."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail[:300]


class FetchFn(Protocol):
    def __call__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        block_detector: BlockDetector | None = None,
    ) -> Awaitable[httpx.Response]: ...


class Backend(Protocol):
    name: str

    async def search(
        self, query: str, count: int, lang: str, fetch: FetchFn
    ) -> list[Hit]: ...


def load_backends() -> dict[str, Backend]:
    """Entry points, plus the two built-ins when the package runs from source.

    Entry points cover an installed package; scanning only those would make
    ``python -m websearch`` in a checkout silently backend-less.
    """
    found: dict[str, Backend] = {}
    try:
        entries = importlib.metadata.entry_points(group="websearch.backends")
    except Exception:  # metadata can be absent when run as a plain directory
        entries = []
    for entry in entries:
        try:
            factory = entry.load()
            instance = factory() if callable(factory) else factory
        except Exception as exc:  # a broken plugin must not take the service down
            print(f"websearch: backend {entry.name} failed to load: {exc}", flush=True)
            continue
        found[instance.name] = instance

    # Built-ins are always available: they are the shipped defaults, not plugins.
    from .ddg import DdgBackend
    from .mwmbl import MwmblBackend

    for instance in (DdgBackend(), MwmblBackend()):
        found.setdefault(instance.name, instance)
    return found
