"""State persistence, locking and health-check support."""

from __future__ import annotations

import fcntl
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import AlreadyRunningError, ConfigError
from .models import StateSnapshot
from .utils import ensure_parent_dir


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat()


def _parse_iso8601(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class FileLock:
    """Exclusive process-level lock backed by flock()."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any | None = None

    def acquire(self) -> None:
        ensure_parent_dir(self.path)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AlreadyRunningError(f"Another StopLiga process already holds {self.path}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class StateStore:
    """Persist operational state atomically for observability and health checks."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"State file is not valid JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ConfigError(f"State file root must be a JSON object: {self.path}")
        return payload

    def write(self, snapshot: StateSnapshot) -> None:
        ensure_parent_dir(self.path)
        payload = asdict(snapshot)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)

    def healthcheck(self, max_age_seconds: int) -> tuple[bool, str]:
        state = self.load()
        last_success = state.get("last_success_at")
        status = state.get("status")
        if not last_success or status not in {"success", "dry_run"}:
            return False, "no recent successful run in state file"

        age_seconds = (datetime.now(timezone.utc) - _parse_iso8601(str(last_success))).total_seconds()
        if age_seconds > max_age_seconds:
            return False, f"last successful run is stale ({int(age_seconds)}s > {max_age_seconds}s)"
        return True, f"healthy, last successful run {int(age_seconds)}s ago"
