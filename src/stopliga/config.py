"""Configuration loading and CLI parsing."""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .models import Config


DEFAULTS = Config()


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ConfigError(f"Invalid boolean value for {field_name}: {value!r}")


def _parse_int(value: Any, *, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer value for {field_name}: {value!r}") from exc


def _parse_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid float value for {field_name}: {value!r}") from exc


def _parse_path(value: Any, *, field_name: str) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    raise ConfigError(f"Invalid path value for {field_name}: {value!r}")


def _normalize_destination_field(value: Any) -> str:
    if value is None:
        return DEFAULTS.destination_field
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("destination_field must be a non-empty string")
    return value.strip()


def _parse_csv_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return tuple(items)
    raise ConfigError(f"Invalid list value for {field_name}: {value!r}")


def load_config_file(path: Path | None) -> dict[str, Any]:
    """Load an optional TOML configuration file."""

    if path is None:
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Configuration file is not valid TOML: {path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration file root must be a mapping: {path}")

    app = raw.get("app", {})
    unifi = raw.get("unifi", {})
    feeds = raw.get("feeds", {})
    bootstrap = raw.get("bootstrap", {})
    if not all(isinstance(section, dict) for section in (app, unifi, feeds, bootstrap)):
        raise ConfigError("Config sections app/unifi/feeds/bootstrap must be TOML tables")

    return {
        "run_mode": app.get("run_mode"),
        "host": unifi.get("host"),
        "port": unifi.get("port"),
        "api_key": unifi.get("api_key"),
        "username": unifi.get("username"),
        "password": unifi.get("password"),
        "site": unifi.get("site"),
        "route_name": app.get("route_name"),
        "destination_field": app.get("destination_field"),
        "status_url": feeds.get("status_url"),
        "ip_list_url": feeds.get("ip_list_url"),
        "enable_when_blocked": app.get("enable_when_blocked"),
        "unifi_verify_tls": unifi.get("verify_tls"),
        "unifi_ca_file": unifi.get("ca_file"),
        "feed_verify_tls": feeds.get("verify_tls"),
        "feed_ca_file": feeds.get("ca_file"),
        "request_timeout": app.get("request_timeout"),
        "retries": app.get("retries"),
        "interval_seconds": app.get("interval_seconds"),
        "dry_run": app.get("dry_run"),
        "invalid_entry_policy": app.get("invalid_entry_policy"),
        "max_destinations": app.get("max_destinations"),
        "state_file": app.get("state_file"),
        "lock_file": app.get("lock_file"),
        "health_max_age_seconds": app.get("health_max_age_seconds"),
        "log_level": app.get("log_level"),
        "vpn_name": bootstrap.get("vpn_name"),
        "target_clients": bootstrap.get("target_clients"),
        "dump_payloads_on_error": app.get("dump_payloads_on_error"),
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""

    parser = argparse.ArgumentParser(
        prog="stopliga",
        description="Synchronize a UniFi policy-based route with a public GitHub IP feed.",
    )
    parser.add_argument("--config", default=None, help="Optional TOML config file")
    parser.add_argument("--healthcheck", action="store_true", help="Validate recent state file freshness")
    parser.add_argument("--host", default=None, help="UniFi console host or IP for local mode")
    parser.add_argument("--port", type=int, default=None, help="UniFi HTTPS port for local mode")
    parser.add_argument("--api-key", default=None, help="UniFi local API key")
    parser.add_argument("--username", default=None, help="UniFi local username")
    parser.add_argument("--password", default=None, help="UniFi local password")
    parser.add_argument("--site", default=None, help="UniFi site name or identifier")
    parser.add_argument("--route-name", default=None, help="Exact route name to manage")
    parser.add_argument("--destination-field", default=None, help="Destination field path or 'auto'")
    parser.add_argument("--status-url", default=None, help="Status JSON URL")
    parser.add_argument("--ip-list-url", default=None, help="IP list TXT URL")
    parser.add_argument("--state-file", default=None, help="State file path")
    parser.add_argument("--lock-file", default=None, help="Lock file path")
    parser.add_argument("--ca-file", dest="unifi_ca_file", default=None, help="CA bundle for UniFi TLS")
    parser.add_argument("--vpn-name", default=None, help="Exact VPN client network name for automatic route creation")
    parser.add_argument("--targets", default=None, help="Comma-separated client names or MACs for automatic route creation")
    parser.add_argument(
        "--invalid-entry-policy",
        choices=["fail", "ignore"],
        default=None,
        help="How to handle invalid feed entries",
    )
    parser.add_argument("--interval", dest="interval_seconds", type=int, default=None, help="Loop interval seconds")
    parser.add_argument("--request-timeout", type=float, default=None, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=None, help="Retry count for transient network errors")
    parser.add_argument("--max-destinations", type=int, default=None, help="Safety ceiling for IP entries")
    parser.add_argument("--health-max-age", dest="health_max_age_seconds", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", default=None, help="Compute changes without writing")
    parser.add_argument(
        "--dump-payloads-on-error",
        action="store_true",
        default=None,
        help="Log truncated route payloads when shape validation fails",
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None)
    parser.add_argument("--verbose", action="store_true", help="Shortcut for --log-level DEBUG")
    parser.add_argument("--disable-when-blocked", action="store_true", default=None)

    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--once", action="store_true", default=None, help="Run a single sync and exit")
    run_group.add_argument("--loop", action="store_true", default=None, help="Run continuously")

    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument("--verify-tls", dest="unifi_verify_tls", action="store_true", default=None)
    tls_group.add_argument("--insecure-skip-verify", dest="unifi_verify_tls", action="store_false", default=None)
    return parser


def _env_value(environ: Mapping[str, str], key: str) -> str | None:
    value = environ.get(key)
    if value is None or value == "":
        return None
    return value


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _normalize_run_mode(args: argparse.Namespace, env: Mapping[str, str], file_cfg: Mapping[str, Any]) -> str:
    if args.loop:
        return "loop"
    if args.once:
        return "once"
    env_mode = _env_value(env, "STOPLIGA_RUN_MODE")
    selected = _first(env_mode, file_cfg.get("run_mode"), DEFAULTS.run_mode)
    if selected not in {"once", "loop"}:
        raise ConfigError(f"Invalid run mode: {selected!r}")
    return str(selected)


def load_config(args: argparse.Namespace, environ: Mapping[str, str] | None = None, *, validate: bool = True) -> Config:
    """Build the runtime configuration with default/file/env/CLI precedence."""

    env = environ or os.environ
    config_path_raw = _first(args.config, _env_value(env, "STOPLIGA_CONFIG_FILE"))
    config_path = Path(config_path_raw).expanduser() if config_path_raw else None
    file_cfg = load_config_file(config_path)

    enable_when_blocked = not bool(args.disable_when_blocked)
    if args.disable_when_blocked is None:
        enable_when_blocked = _parse_bool(
            _first(_env_value(env, "STOPLIGA_ENABLE_WHEN_BLOCKED"), file_cfg.get("enable_when_blocked"), DEFAULTS.enable_when_blocked),
            field_name="enable_when_blocked",
        )

    log_level = "DEBUG" if args.verbose else _first(
        args.log_level,
        _env_value(env, "STOPLIGA_LOG_LEVEL"),
        file_cfg.get("log_level"),
        DEFAULTS.log_level,
    )

    config = Config(
        run_mode=_normalize_run_mode(args, env, file_cfg),
        host=_first(args.host, _env_value(env, "UNIFI_HOST"), file_cfg.get("host"), DEFAULTS.host),
        port=_parse_int(_first(args.port, _env_value(env, "UNIFI_PORT"), file_cfg.get("port"), DEFAULTS.port), field_name="port"),
        api_key=_first(args.api_key, _env_value(env, "UNIFI_API_KEY"), file_cfg.get("api_key"), DEFAULTS.api_key),
        username=_first(args.username, _env_value(env, "UNIFI_USERNAME"), file_cfg.get("username"), DEFAULTS.username),
        password=_first(args.password, _env_value(env, "UNIFI_PASSWORD"), file_cfg.get("password"), DEFAULTS.password),
        site=str(_first(args.site, _env_value(env, "UNIFI_SITE"), file_cfg.get("site"), DEFAULTS.site)),
        route_name=str(_first(args.route_name, _env_value(env, "STOPLIGA_ROUTE_NAME"), file_cfg.get("route_name"), DEFAULTS.route_name)),
        destination_field=_normalize_destination_field(
            _first(args.destination_field, _env_value(env, "STOPLIGA_DESTINATION_FIELD"), file_cfg.get("destination_field"), DEFAULTS.destination_field)
        ),
        status_url=str(_first(args.status_url, _env_value(env, "STOPLIGA_STATUS_URL"), file_cfg.get("status_url"), DEFAULTS.status_url)),
        ip_list_url=str(_first(args.ip_list_url, _env_value(env, "STOPLIGA_IP_LIST_URL"), file_cfg.get("ip_list_url"), DEFAULTS.ip_list_url)),
        enable_when_blocked=enable_when_blocked,
        unifi_verify_tls=_parse_bool(
            _first(args.unifi_verify_tls, _env_value(env, "UNIFI_VERIFY_TLS"), file_cfg.get("unifi_verify_tls"), DEFAULTS.unifi_verify_tls),
            field_name="unifi_verify_tls",
        ),
        unifi_ca_file=_parse_path(value, field_name="unifi_ca_file") if (value := _first(args.unifi_ca_file, _env_value(env, "UNIFI_CA_FILE"), file_cfg.get("unifi_ca_file"))) else None,
        feed_verify_tls=_parse_bool(
            _first(_env_value(env, "STOPLIGA_FEED_VERIFY_TLS"), file_cfg.get("feed_verify_tls"), DEFAULTS.feed_verify_tls),
            field_name="feed_verify_tls",
        ),
        feed_ca_file=_parse_path(value, field_name="feed_ca_file") if (value := _first(_env_value(env, "STOPLIGA_FEED_CA_FILE"), file_cfg.get("feed_ca_file"))) else None,
        request_timeout=_parse_float(
            _first(args.request_timeout, _env_value(env, "STOPLIGA_REQUEST_TIMEOUT"), file_cfg.get("request_timeout"), DEFAULTS.request_timeout),
            field_name="request_timeout",
        ),
        retries=_parse_int(
            _first(args.retries, _env_value(env, "STOPLIGA_RETRIES"), file_cfg.get("retries"), DEFAULTS.retries),
            field_name="retries",
        ),
        interval_seconds=_parse_int(
            _first(args.interval_seconds, _env_value(env, "STOPLIGA_SYNC_INTERVAL_SECONDS"), file_cfg.get("interval_seconds"), DEFAULTS.interval_seconds),
            field_name="interval_seconds",
        ),
        dry_run=_parse_bool(
            _first(args.dry_run, _env_value(env, "STOPLIGA_DRY_RUN"), file_cfg.get("dry_run"), DEFAULTS.dry_run),
            field_name="dry_run",
        ),
        invalid_entry_policy=str(
            _first(args.invalid_entry_policy, _env_value(env, "STOPLIGA_INVALID_ENTRY_POLICY"), file_cfg.get("invalid_entry_policy"), DEFAULTS.invalid_entry_policy)
        ),
        max_destinations=_parse_int(
            _first(args.max_destinations, _env_value(env, "STOPLIGA_MAX_DESTINATIONS"), file_cfg.get("max_destinations"), DEFAULTS.max_destinations),
            field_name="max_destinations",
        ),
        state_file=_parse_path(
            _first(args.state_file, _env_value(env, "STOPLIGA_STATE_FILE"), file_cfg.get("state_file"), str(DEFAULTS.state_file)),
            field_name="state_file",
        ),
        lock_file=_parse_path(
            _first(args.lock_file, _env_value(env, "STOPLIGA_LOCK_FILE"), file_cfg.get("lock_file"), str(DEFAULTS.lock_file)),
            field_name="lock_file",
        ),
        health_max_age_seconds=_parse_int(value, field_name="health_max_age_seconds") if (value := _first(args.health_max_age_seconds, _env_value(env, "STOPLIGA_HEALTH_MAX_AGE_SECONDS"), file_cfg.get("health_max_age_seconds"))) is not None else None,
        log_level=str(log_level).upper(),
        vpn_name=_first(args.vpn_name, _env_value(env, "STOPLIGA_VPN_NAME"), file_cfg.get("vpn_name"), DEFAULTS.vpn_name),
        target_clients=_parse_csv_list(
            _first(args.targets, _env_value(env, "STOPLIGA_TARGETS"), file_cfg.get("target_clients"), DEFAULTS.target_clients),
            field_name="target_clients",
        ),
        dump_payloads_on_error=_parse_bool(
            _first(args.dump_payloads_on_error, _env_value(env, "STOPLIGA_DUMP_PAYLOADS_ON_ERROR"), file_cfg.get("dump_payloads_on_error"), DEFAULTS.dump_payloads_on_error),
            field_name="dump_payloads_on_error",
        ),
    )

    validate_config(config, validate_connection=validate and not args.healthcheck)
    return config


def validate_config(config: Config, *, validate_connection: bool) -> None:
    """Validate configuration invariants."""

    if config.retries < 1:
        raise ConfigError("retries must be >= 1")
    if config.request_timeout <= 0:
        raise ConfigError("request_timeout must be > 0")
    if config.interval_seconds <= 0 and config.run_mode == "loop":
        raise ConfigError("loop mode requires interval_seconds > 0")
    if config.max_destinations < 1:
        raise ConfigError("max_destinations must be >= 1")
    if config.invalid_entry_policy not in {"fail", "ignore"}:
        raise ConfigError(f"invalid_entry_policy must be fail|ignore, not {config.invalid_entry_policy!r}")
    if bool(config.vpn_name) != bool(config.target_clients):
        raise ConfigError("Automatic route creation requires both STOPLIGA_VPN_NAME and STOPLIGA_TARGETS")
    if validate_connection:
        if not config.has_unifi_auth():
            raise ConfigError("local mode requires UNIFI_HOST and either UNIFI_API_KEY or UNIFI_USERNAME/UNIFI_PASSWORD")
