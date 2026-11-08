"""Router driver selection."""

from __future__ import annotations

from ..errors import ConfigError
from ..models import Config
from .base import RouterDriver


def create_router_driver(config: Config) -> RouterDriver:
    """Create the router driver selected by configuration."""

    if config.router_type == "unifi":
        from .unifi import UniFiRouterDriver

        return UniFiRouterDriver(config)
    if config.router_type == "omada":
        from .omada import OmadaRouterDriver

        return OmadaRouterDriver(config)
    if config.router_type == "opnsense":
        from .opnsense import OPNsenseRouterDriver

        return OPNsenseRouterDriver(config)
    raise ConfigError(f"Unsupported router_type {config.router_type!r}")
