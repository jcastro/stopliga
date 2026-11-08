"""Optional local-time windows; overnight windows belong to their starting day."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError
from .models import Config, FeedSnapshot


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ActiveSchedule:
    def __init__(self, config: Config):
        for name in ("active_days", "active_hours", "active_timezone"):
            value = getattr(config, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(f"{name} must be a non-empty string when set")
        self.configured = bool(config.active_days or config.active_hours)
        self.days = set(range(7))
        if config.active_days:
            tokens = config.active_days.lower().split(",")
            if any(token.strip() not in DAYS for token in tokens):
                raise ConfigError("active_days must be comma-separated mon,tue,wed,thu,fri,sat,sun")
            self.days = {DAYS.index(token.strip()) for token in tokens}
        self.windows: list[tuple[int, int]] = []
        if config.active_hours:
            for window in config.active_hours.split(","):
                match = re.fullmatch(r"\s*(\d{2}):(\d{2})-(\d{2}):(\d{2})\s*", window)
                if not match:
                    raise ConfigError("active_hours must use HH:MM-HH:MM, optionally comma-separated")
                sh, sm, eh, em = map(int, match.groups())
                if sh > 23 or eh > 23 or sm > 59 or em > 59 or (sh, sm) == (eh, em):
                    raise ConfigError("active_hours needs valid, distinct start and end times")
                self.windows.append((sh * 60 + sm, eh * 60 + em))
        try:
            self.zone = ZoneInfo(config.active_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ConfigError("active_timezone must be a valid IANA timezone, e.g. Europe/Madrid") from exc

    def is_active(self, now: datetime | None = None) -> bool:
        if not self.configured:
            return True
        local = (now or datetime.now(timezone.utc)).astimezone(self.zone)
        if not self.windows:
            return local.weekday() in self.days
        minute = local.hour * 60 + local.minute
        for start, end in self.windows:
            if start < end:
                if local.weekday() in self.days and start <= minute < end:
                    return True
            elif (local.weekday() in self.days and minute >= start) or (
                (local - timedelta(days=1)).weekday() in self.days and minute < end
            ):
                return True
        return False

    def wait_seconds(self, interval: float) -> float:
        if not self.configured:
            return interval
        now = datetime.now(timezone.utc)
        return min(interval, 60 - now.second - now.microsecond / 1_000_000)


def inactive_snapshot() -> FeedSnapshot:
    """Disable managed rules without depending on remote feed availability."""
    return FeedSnapshot(
        is_blocked=False,
        desired_enabled=False,
        destinations=[],
        raw_status={"schedule_active": False},
        raw_line_count=0,
        valid_count=0,
        invalid_count=0,
        invalid_entries=[],
        destinations_hash="schedule-inactive",
        feed_hash="schedule-inactive",
    )
