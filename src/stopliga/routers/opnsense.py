"""OPNsense router driver wrapper."""

from __future__ import annotations


from ..models import Config, FeedSnapshot, SyncResult
from ..opnsense import OPNsenseClient, sync_opnsense
from .base import BeforeWrite, BootstrapGuardClearer, BootstrapGuardWriter, RouterDriver


class OPNsenseRouterDriver(RouterDriver):
    """RouterDriver adapter for the OPNsense backend."""

    router_type = "opnsense"

    def __init__(self, config: Config):
        self.config = config
        self.client = OPNsenseClient(config)

    def sync(
        self,
        feed_snapshot: FeedSnapshot,
        previous_guard: dict[str, object],
        *,
        guard_writer: BootstrapGuardWriter,
        guard_clearer: BootstrapGuardClearer,
        before_write: BeforeWrite | None = None,
    ) -> SyncResult:
        del previous_guard
        del guard_writer
        del guard_clearer
        self.client.before_write = before_write
        return sync_opnsense(self.config, feed_snapshot, client=self.client)
