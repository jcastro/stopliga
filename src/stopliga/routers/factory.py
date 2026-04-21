"""Router driver selection."""

from __future__ import annotations

from ..errors import ConfigError
from ..models import Config
from .base import RouterDriver
from .omada import OmadaRouterDriver
from .opnsense import OPNsenseRouterDriver
from .unifi import UniFiRouterDriver


def create_router_driver(config: Config) -> RouterDriver:
    """Create the router driver selected by configuration."""

    if config.router_type == "unifi":
        return UniFiRouterDriver(config)
    if config.router_type == "omada":
        return OmadaRouterDriver(config)
    if config.router_type == "opnsense":
        return OPNsenseRouterDriver(config)
    raise ConfigError(f"Unsupported router_type {config.router_type!r}")
