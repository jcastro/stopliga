"""OPNsense router driver wrapper."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Config, FeedSnapshot, SyncResult
from ..opnsense import sync_opnsense
from .base import BootstrapGuardClearer, BootstrapGuardWriter, RouterDriver


@dataclass(frozen=True)
class OPNsenseRouterDriver(RouterDriver):
    """RouterDriver adapter for the OPNsense backend."""

    config: Config
    router_type: str = "opnsense"

    def sync(
        self,
        feed_snapshot: FeedSnapshot,
        previous_guard: dict[str, object],
        *,
        guard_writer: BootstrapGuardWriter,
        guard_clearer: BootstrapGuardClearer,
    ) -> SyncResult:
        del previous_guard
        del guard_writer
        del guard_clearer
        return sync_opnsense(self.config, feed_snapshot)
