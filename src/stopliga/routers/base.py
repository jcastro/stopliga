"""Common router driver interfaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..models import FeedSnapshot, SyncResult, UpdatePlan


BootstrapGuardWriter = Callable[[str | None, str | None, tuple[str, ...]], None]
BootstrapGuardClearer = Callable[[], None]


class RouteBackend(Protocol):
    """Backend contract for a single router API surface."""

    backend_name: str

    def build_plan(
        self,
        endpoint: str,
        route_record: dict[str, Any],
        desired_ips: list[str],
        desired_enabled: bool,
    ) -> UpdatePlan:
        """Build the update plan for a specific route."""

    def create_route(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Create a new route and return the collection endpoint plus record."""

    def verify(self, route_id_value: str, desired_ips: list[str], desired_enabled: bool) -> None:
        """Verify that the remote state matches the expected values."""


@dataclass(frozen=True)
class ResolvedRoute:
    """Resolved route data returned by a router driver."""

    backend: RouteBackend
    endpoint: str
    route_record: dict[str, Any]


class RouterDriver(Protocol):
    """High-level router driver used by the sync service."""

    router_type: str

    def sync(
        self,
        feed_snapshot: FeedSnapshot,
        previous_guard: dict[str, object],
        *,
        guard_writer: BootstrapGuardWriter,
        guard_clearer: BootstrapGuardClearer,
    ) -> SyncResult:
        """Synchronize the managed route against the desired feed snapshot."""
