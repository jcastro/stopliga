"""Minimal read-only container health probe, without controllers or credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import tomllib

from .state import StateStore


def main() -> int:
    try:
        app: dict[str, Any] = {}
        config_file = os.environ.get("STOPLIGA_CONFIG_FILE", "/config/config.toml")
        if Path(config_file).is_file():
            with open(config_file, "rb") as handle:
                app = tomllib.load(handle).get("app", {})
        path = Path(os.environ.get("STOPLIGA_STATE_FILE", str(app.get("state_file", "/data/state.json"))))
        mode = os.environ.get("STOPLIGA_RUN_MODE", app.get("run_mode", "once"))
        interval = int(os.environ.get("STOPLIGA_SYNC_INTERVAL_SECONDS", app.get("interval_seconds", 300)))
        default_age = max(300, interval * 3) if mode == "loop" else 86400
        age = int(os.environ.get("STOPLIGA_HEALTH_MAX_AGE_SECONDS", app.get("health_max_age_seconds", default_age)))
        if age <= 0:
            raise ValueError("health max age must be positive")
        healthy, message = StateStore(path).healthcheck(age)
        print(message)
        return 0 if healthy else 1
    except (OSError, ValueError, TypeError) as exc:
        print(f"Invalid healthcheck configuration: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
