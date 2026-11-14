"""Build the runtime objects once, for both the server and the CLI."""

from __future__ import annotations

from .backends.base import load_backends
from .config import Config
from .core import SearchService
from .egress.router import Egress
from .egress.tunnel import Tunnel


def build_service(config: Config) -> SearchService:
    tunnel = Tunnel(config) if config.egress.mode != "direct" else None
    egress = Egress(config, tunnel)
    return SearchService(config, load_backends(), egress, tunnel)
