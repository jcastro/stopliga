"""Historical feed fixtures share a deterministic reference clock."""

from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def feed_clock(monkeypatch):
    monkeypatch.setattr("stopliga.feed._utcnow", lambda: datetime(2026, 4, 23, 9, 30, tzinfo=timezone.utc))
