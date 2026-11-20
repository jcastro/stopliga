"""Minimal read-only container health probe, without controllers or credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import tomllib

from .errors import ConfigError, StateError
from .models import Config
from .state import StateStore


def write_healthcheck_settings(config: Config) -> None:
    """Share effective startup settings with the independent image healthcheck."""
    settings_file = os.environ.get("STOPLIGA_HEALTHCHECK_FILE")
    if not settings_file or config.dry_run:
        return
    path = Path(settings_file).resolve()
    if path in {p.resolve() for p in (config.state_file, config.lock_file, config.bootstrap_guard_file)}:
        raise ConfigError("The healthcheck settings file must be separate from runtime state, lock and bootstrap guard")
    StateStore(path).write_payload(
        {"state_file": str(config.state_file.absolute()), "max_age_seconds": config.resolved_health_max_age()}
    )


def _settings() -> tuple[Path, int]:
    if settings_file := os.environ.get("STOPLIGA_HEALTHCHECK_FILE"):
        settings = StateStore(Path(settings_file)).load()
        path = settings.get("state_file")
        age = settings.get("max_age_seconds")
        if not isinstance(path, str) or not path or type(age) is not int:
            raise ValueError("No valid runtime healthcheck settings are available yet")
        return Path(path), age

    # Keep the standalone probe usable outside images that publish their settings.
    app: dict[str, Any] = {}
    config_file = os.environ.get("STOPLIGA_CONFIG_FILE", "/config/config.toml")
    if Path(config_file).is_file():
        with open(config_file, "rb") as handle:
            app = tomllib.load(handle).get("app", {})
    path = os.environ.get("STOPLIGA_STATE_FILE", str(app.get("state_file", "/data/state.json")))
    mode = os.environ.get("STOPLIGA_RUN_MODE", app.get("run_mode", "once"))
    interval = int(os.environ.get("STOPLIGA_SYNC_INTERVAL_SECONDS", app.get("interval_seconds", 300)))
    default_age = max(300, interval * 3) if mode == "loop" else 86400
    age = int(os.environ.get("STOPLIGA_HEALTH_MAX_AGE_SECONDS", app.get("health_max_age_seconds", default_age)))
    return Path(path), age


def main() -> int:
    try:
        path, age = _settings()
        if age <= 0:
            raise ValueError("health max age must be positive")
        healthy, message = StateStore(path).healthcheck(age)
        print(message)
        return 0 if healthy else 1
    except (ConfigError, StateError, OSError, ValueError, TypeError) as exc:
        print(f"Invalid healthcheck configuration: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
