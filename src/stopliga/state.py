"""State persistence, locking and health-check support."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import AlreadyRunningError, ConfigError, StateError
from .models import StateSnapshot
from .utils import ensure_parent_dir

MAX_STATE_FILE_BYTES = 1024 * 1024


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
        try:
            handle = self.path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise StateError(f"Unable to open lock file {self.path}: {exc}") from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AlreadyRunningError(f"Another StopLiga process already holds {self.path}") from exc
        except OSError as exc:
            handle.close()
            raise StateError(f"Unable to lock {self.path}: {exc}") from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            handle.close()
            raise StateError(f"Unable to write lock metadata to {self.path}: {exc}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise StateError(f"Unable to unlock {self.path}: {exc}") from exc
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.release()
        except StateError:
            if exc_type is not None:
                logging.getLogger("stopliga.state").exception("lock_release_failed", exc_info=True)
                return
            raise


class StateStore:
    """Persist operational state atomically for observability and health checks."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            stat_result = self.path.stat()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StateError(f"Unable to read state file {self.path}: {exc}") from exc
        if stat_result.st_size > MAX_STATE_FILE_BYTES:
            raise ConfigError(f"State file exceeds safety limit of {MAX_STATE_FILE_BYTES} bytes: {self.path}")
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateError(f"Unable to read state file {self.path}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"State file is not valid JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ConfigError(f"State file root must be a JSON object: {self.path}")
        return payload

    def quarantine_invalid_file(self) -> Path | None:
        """Move a malformed state file aside so runtime sync can continue."""

        if not self.path.exists():
            return None
        bad_name = f"{self.path.name}.bad-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        bad_path = self.path.with_name(bad_name)
        try:
            os.replace(self.path, bad_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateError(f"Unable to quarantine invalid state file {self.path}: {exc}") from exc
        return bad_path

    def write(self, snapshot: StateSnapshot) -> None:
        ensure_parent_dir(self.path)
        payload = dict(vars(snapshot))
        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f"{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except OSError as exc:
            raise StateError(f"Unable to write state file {self.path}: {exc}") from exc
        finally:
            if temp_name and os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except OSError:
                    logging.getLogger("stopliga.state").warning("state_temp_cleanup_failed", exc_info=True)

    def healthcheck(self, max_age_seconds: int) -> tuple[bool, str]:
        try:
            state = self.load()
        except (ConfigError, StateError) as exc:
            return False, str(exc)
        last_success = state.get("last_success_at")
        status = state.get("status")
        consecutive_failures = state.get("consecutive_failures", 0)
        if state.get("reconciliation_required"):
            return False, "runtime state requires reconciliation before further writes"
        if not last_success:
            return False, "no recent successful run in state file"
        if status not in {"success", "dry_run"}:
            return False, f"last run status is {status!r} with consecutive_failures={consecutive_failures}"

        try:
            age_seconds = (datetime.now(timezone.utc) - _parse_iso8601(str(last_success))).total_seconds()
        except ValueError:
            return False, f"state file contains an invalid last_success_at: {last_success!r}"
        if age_seconds < -60:
            return False, f"state file last_success_at is in the future ({int(age_seconds)}s)"
        if age_seconds > max_age_seconds:
            return False, f"last successful run is stale ({int(age_seconds)}s > {max_age_seconds}s)"
        return True, f"healthy, last successful run {int(age_seconds)}s ago"
