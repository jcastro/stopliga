"""Configuration loading and CLI parsing."""

from __future__ import annotations

import argparse
import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping, cast, get_args
from urllib.parse import urlparse

from .errors import ConfigError
from .models import Config, InvalidEntryPolicy, LegacyFirewallBackend, OmadaTargetType, RouterType, RunMode


DEFAULTS = Config()
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


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
    if isinstance(value, bool):
        raise ConfigError(f"Invalid integer value for {field_name}: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer value for {field_name}: {value!r}") from exc


def _parse_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"Invalid float value for {field_name}: {value!r}")
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


def _validate_log_level(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"log_level must be one of {', '.join(sorted(VALID_LOG_LEVELS))}")
    normalized = value.strip().upper()
    if normalized not in VALID_LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(sorted(VALID_LOG_LEVELS))}, not {value!r}")
    return normalized


def _validate_host(host: str, *, field_name: str) -> None:
    candidate = host.strip()
    if not candidate:
        raise ConfigError(f"{field_name} must not be empty")
    if candidate != host:
        raise ConfigError(f"{field_name} must not contain leading or trailing whitespace")
    if "://" in candidate or "/" in candidate or "@" in candidate or "?" in candidate or "#" in candidate:
        raise ConfigError(f"{field_name} must be a hostname or IP address without scheme, path or credentials")
    if candidate.startswith("[") and candidate.endswith("]"):
        inner = candidate[1:-1]
        try:
            ipaddress.IPv6Address(inner)
        except ipaddress.AddressValueError as exc:
            raise ConfigError(f"{field_name} contains an invalid bracketed IPv6 address: {candidate!r}") from exc
        return
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
        if any(ch not in allowed for ch in candidate):
            raise ConfigError(f"{field_name} contains unsupported characters: {candidate!r}")
        if (
            ".." in candidate
            or candidate.startswith(".")
            or candidate.endswith(".")
            or candidate.startswith("-")
            or candidate.endswith("-")
        ):
            raise ConfigError(f"{field_name} is not a valid hostname: {candidate!r}")
    else:
        return


def _normalize_destination_field(value: Any) -> str:
    if value is None:
        return DEFAULTS.destination_field
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("destination_field must be a non-empty string")
    return value.strip()


def _is_private_hostname(hostname: str) -> bool:
    lowered = hostname.strip().lower()
    if lowered in {"localhost"}:
        return True
    try:
        ip_value = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(ip_value.is_private or ip_value.is_loopback or ip_value.is_link_local or ip_value.is_reserved)


def _validate_feed_url(
    url: str,
    *,
    field_name: str,
    allow_private_hosts: bool,
    allow_dns: bool = False,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "dns":
        if not allow_dns:
            raise ConfigError(f"{field_name} must use http or https, not {parsed.scheme!r}")
        if not parsed.hostname:
            raise ConfigError(f"{field_name} must include a hostname")
        if parsed.username or parsed.password:
            raise ConfigError(f"{field_name} must not embed credentials")
        if parsed.port is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ConfigError(f"{field_name} DNS URLs must look like dns://hostname")
        return
    if parsed.scheme not in {"https", "http"}:
        raise ConfigError(f"{field_name} must use http or https, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigError(f"{field_name} must include a hostname")
    if parsed.username or parsed.password:
        raise ConfigError(f"{field_name} must not embed credentials")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ConfigError(f"{field_name} only allows plain HTTP for localhost/127.0.0.1")
    if (
        _is_private_hostname(parsed.hostname)
        and not allow_private_hosts
        and parsed.hostname not in {"127.0.0.1", "localhost"}
    ):
        raise ConfigError(f"{field_name} points to a private or local host; set feed_allow_private_hosts to override")


def _validate_notification_url(url: str, *, field_name: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ConfigError(f"{field_name} must use http or https, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigError(f"{field_name} must include a hostname")
    if parsed.username or parsed.password:
        raise ConfigError(f"{field_name} must not embed credentials")


def _validate_gotify_url(url: str, *, allow_plain_http: bool) -> None:
    _validate_notification_url(url, field_name="gotify_url")
    parsed = urlparse(url)
    if parsed.scheme == "http" and not allow_plain_http:
        raise ConfigError("gotify_url must use https unless STOPLIGA_GOTIFY_ALLOW_PLAIN_HTTP=true")


def _validate_api_base_url(url: str, *, field_name: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ConfigError(f"{field_name} must use http or https, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigError(f"{field_name} must include a hostname")
    if parsed.username or parsed.password:
        raise ConfigError(f"{field_name} must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{field_name} must not include query or fragment components")


def _normalize_omada_base_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("omada_base_url must be a non-empty URL")
    normalized = value.strip().rstrip("/")
    if normalized.endswith("/openapi"):
        normalized = normalized[:-8]
    return normalized


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
    controller = raw.get("controller", {})
    unifi = raw.get("unifi", {})
    omada = raw.get("omada", {})
    keenetic = raw.get("keenetic", {})
    opnsense = raw.get("opnsense", {})
    feeds = raw.get("feeds", {})
    bootstrap = raw.get("bootstrap", {})
    notifications = raw.get("notifications", {})
    if not all(
        isinstance(section, dict)
        for section in (app, controller, unifi, omada, keenetic, opnsense, feeds, bootstrap, notifications)
    ):
        raise ConfigError(
            "Config sections app/controller/unifi/omada/keenetic/opnsense/feeds/bootstrap/notifications must be TOML tables"
        )

    return {
        "run_mode": app.get("run_mode"),
        "backend": app.get("backend"),
        "controller": app.get("controller"),
        "router_type": app.get("router_type"),
        "firewall_backend": app.get("firewall_backend"),
        "controller_host": controller.get("host"),
        "controller_port": controller.get("port"),
        "controller_site": controller.get("site"),
        "controller_verify_tls": controller.get("verify_tls"),
        "controller_ca_file": controller.get("ca_file"),
        "unifi_host": unifi.get("host"),
        "unifi_port": unifi.get("port"),
        "api_key": unifi.get("api_key"),
        "unifi_site": unifi.get("site"),
        "omada_host": omada.get("host"),
        "omada_port": omada.get("port"),
        "omada_site": omada.get("site"),
        "route_name": app.get("route_name"),
        "destination_field": app.get("destination_field"),
        "omada_base_url": omada.get("base_url"),
        "omada_client_id": omada.get("client_id"),
        "omada_client_secret": omada.get("client_secret"),
        "omada_omadac_id": omada.get("omadac_id"),
        "omada_target_type": omada.get("target_type"),
        "omada_target": omada.get("target"),
        "omada_source_networks": omada.get("source_networks"),
        "omada_verify_tls": omada.get("verify_tls"),
        "omada_ca_file": omada.get("ca_file"),
        "omada_group_size": omada.get("group_size"),
        "keenetic_base_url": keenetic.get("base_url"),
        "keenetic_username": keenetic.get("username"),
        "keenetic_password": keenetic.get("password"),
        "keenetic_verify_tls": keenetic.get("verify_tls"),
        "keenetic_ca_file": keenetic.get("ca_file"),
        "keenetic_interface": keenetic.get("interface"),
        "keenetic_gateway": keenetic.get("gateway"),
        "keenetic_auto": keenetic.get("auto"),
        "keenetic_reject": keenetic.get("reject"),
        "status_url": feeds.get("status_url"),
        "ip_list_url": feeds.get("ip_list_url"),
        "unifi_verify_tls": unifi.get("verify_tls"),
        "unifi_ca_file": unifi.get("ca_file"),
        "opnsense_host": opnsense.get("host"),
        "opnsense_api_key": opnsense.get("api_key"),
        "opnsense_api_secret": opnsense.get("api_secret"),
        "opnsense_verify_tls": opnsense.get("verify_tls"),
        "opnsense_ca_file": opnsense.get("ca_file"),
        "opnsense_alias_name": opnsense.get("alias_name"),
        "feed_verify_tls": feeds.get("verify_tls"),
        "feed_ca_file": feeds.get("ca_file"),
        "feed_allow_private_hosts": feeds.get("allow_private_hosts"),
        "strict_feed_consistency": feeds.get("strict_consistency"),
        "request_timeout": app.get("request_timeout"),
        "retries": app.get("retries"),
        "max_response_bytes": app.get("max_response_bytes"),
        "interval_seconds": app.get("interval_seconds"),
        "dry_run": app.get("dry_run"),
        "invalid_entry_policy": app.get("invalid_entry_policy"),
        "max_destinations": app.get("max_destinations"),
        "state_file": app.get("state_file"),
        "lock_file": app.get("lock_file"),
        "bootstrap_guard_file": app.get("bootstrap_guard_file"),
        "health_max_age_seconds": app.get("health_max_age_seconds"),
        "log_level": app.get("log_level"),
        "vpn_name": bootstrap.get("vpn_name"),
        "target_clients": bootstrap.get("target_clients"),
        "dump_payloads_on_error": app.get("dump_payloads_on_error"),
        "gotify_url": notifications.get("gotify_url"),
        "gotify_token": notifications.get("gotify_token"),
        "gotify_priority": notifications.get("gotify_priority"),
        "telegram_bot_token": notifications.get("telegram_bot_token"),
        "telegram_chat_id": notifications.get("telegram_chat_id"),
        "telegram_group_id": notifications.get("telegram_group_id"),
        "telegram_topic_id": notifications.get("telegram_topic_id"),
        "notification_timeout": notifications.get("timeout"),
        "notification_retries": notifications.get("retries"),
        "notification_verify_tls": notifications.get("verify_tls"),
        "notification_ca_file": notifications.get("ca_file"),
        "gotify_verify_tls": notifications.get("gotify_verify_tls"),
        "gotify_ca_file": notifications.get("gotify_ca_file"),
        "gotify_allow_plain_http": notifications.get("gotify_allow_plain_http"),
        "telegram_verify_tls": notifications.get("telegram_verify_tls"),
        "telegram_ca_file": notifications.get("telegram_ca_file"),
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""

    parser = argparse.ArgumentParser(
        prog="stopliga",
        description="Synchronize a managed router route with live block status and a public IP feed.",
    )
    parser.add_argument("--config", default=None, help="Optional TOML config file")
    parser.add_argument("--healthcheck", action="store_true", help="Validate recent state file freshness")
    parser.add_argument("--router-type", default=None, help="Router backend to use")
    parser.add_argument("--host", default=None, help="UniFi console host or IP for local mode")
    parser.add_argument("--port", type=int, default=None, help="UniFi HTTPS port for local mode")
    parser.add_argument("--api-key", default=None, help="UniFi local API key")
    parser.add_argument("--site", default=None, help="UniFi site name or identifier")
    parser.add_argument("--route-name", default=None, help="Exact route name to manage or create")
    parser.add_argument("--destination-field", default=None, help="Destination field path or 'auto'")
    parser.add_argument("--omada-base-url", default=None, help="Omada Controller interface access URL")
    parser.add_argument("--omada-client-id", default=None, help="Omada Open API client ID")
    parser.add_argument("--omada-client-secret", default=None, help="Omada Open API client secret")
    parser.add_argument("--omadac-id", dest="omada_omadac_id", default=None, help="Omada cloud/controller ID")
    parser.add_argument("--omada-target-type", choices=["wan", "vpn"], default=None, help="Omada egress target kind")
    parser.add_argument("--omada-target", default=None, help="Omada WAN or VPN name/ID to route through")
    parser.add_argument("--omada-source-networks", default=None, help="Comma-separated Omada LAN network names or IDs")
    parser.add_argument(
        "--omada-group-size", type=int, default=None, help="Maximum IPv4 subnets per managed Omada IP Group"
    )
    parser.add_argument(
        "--keenetic-base-url", default=None, help="Keenetic RCI base URL, for example http://192.168.1.1"
    )
    parser.add_argument("--keenetic-username", default=None, help="Keenetic username")
    parser.add_argument("--keenetic-password", default=None, help="Keenetic password")
    parser.add_argument("--keenetic-interface", default=None, help="Keenetic interface used for managed static routes")
    parser.add_argument("--keenetic-gateway", default=None, help="Optional Keenetic next-hop gateway")
    parser.add_argument("--status-url", default=None, help="Status feed URL (http/https or dns://hostname)")
    parser.add_argument("--ip-list-url", default=None, help="IP list TXT URL")
    parser.add_argument("--state-file", default=None, help="State file path")
    parser.add_argument("--lock-file", default=None, help="Lock file path")
    parser.add_argument("--ca-file", dest="unifi_ca_file", default=None, help="CA bundle for UniFi TLS")
    parser.add_argument("--omada-ca-file", default=None, help="CA bundle for Omada TLS")
    parser.add_argument("--keenetic-ca-file", default=None, help="CA bundle for Keenetic TLS")
    parser.add_argument("--vpn-name", default=None, help="Exact VPN client network name for automatic route creation")
    parser.add_argument(
        "--targets", default=None, help="Comma-separated client names or MACs for automatic route creation"
    )
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
    parser.add_argument("--max-response-bytes", type=int, default=None, help="Maximum HTTP response body size in bytes")
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
    parser.add_argument("--gotify-url", default=None, help="Gotify server URL")
    parser.add_argument("--gotify-token", default=None, help="Gotify application token")
    parser.add_argument("--gotify-priority", type=int, default=None, help="Gotify priority")
    parser.add_argument("--telegram-bot-token", default=None, help="Telegram bot token")
    parser.add_argument("--telegram-chat-id", default=None, help="Telegram user/chat id")
    parser.add_argument("--telegram-group-id", default=None, help="Telegram group or supergroup id")
    parser.add_argument("--telegram-topic-id", type=int, default=None, help="Telegram forum topic id")
    parser.add_argument("--notification-timeout", type=float, default=None, help="Notification HTTP timeout in seconds")
    parser.add_argument("--notification-retries", type=int, default=None, help="Notification retry count")

    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--once", action="store_true", default=None, help="Run a single sync and exit")
    run_group.add_argument("--loop", action="store_true", default=None, help="Run continuously")

    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument("--verify-tls", dest="unifi_verify_tls", action="store_true", default=None)
    tls_group.add_argument("--insecure-skip-verify", dest="unifi_verify_tls", action="store_false", default=None)

    omada_tls_group = parser.add_mutually_exclusive_group()
    omada_tls_group.add_argument("--omada-verify-tls", dest="omada_verify_tls", action="store_true", default=None)
    omada_tls_group.add_argument(
        "--omada-insecure-skip-verify", dest="omada_verify_tls", action="store_false", default=None
    )
    return parser


def _env_value(environ: Mapping[str, str], key: str) -> str | None:
    value = environ.get(key)
    if value is None or value == "":
        return None
    return value


def _secret_env_value(environ: Mapping[str, str], key: str) -> str | None:
    return _env_value(environ, key)


def _secret_file_value(environ: Mapping[str, str], key_file: str, *, field_name: str) -> str | None:
    path_value = _env_value(environ, key_file)
    if path_value is None:
        return None
    path = Path(path_value).expanduser()
    if path.exists() and not path.is_file():
        raise ConfigError(f"Secret file for {field_name} must be a regular file: {path}")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Unable to read secret file for {field_name}: {path_value}") from exc
    if not secret:
        raise ConfigError(f"Secret file for {field_name} is empty: {path_value}")
    return secret


def _env_secret_first(environ: Mapping[str, str], *, field_name: str, key: str, key_file: str) -> str | None:
    direct = _secret_env_value(environ, key)
    from_file = _secret_file_value(environ, key_file, field_name=field_name)
    if direct is not None and from_file is not None:
        raise ConfigError(f"Set either {key} or {key_file} for {field_name}, not both")
    return direct if direct is not None else from_file


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _format_https_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _build_controller_base_url(host: str, port: int) -> str:
    return f"https://{_format_https_host(host)}:{port}"


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
    router_type = cast(
        RouterType,
        str(
            _first(
                args.router_type,
                _env_value(env, "STOPLIGA_BACKEND"),
                _env_value(env, "STOPLIGA_CONTROLLER"),
                _env_value(env, "STOPLIGA_ROUTER_TYPE"),
                _env_value(env, "STOPLIGA_FIREWALL_BACKEND"),
                file_cfg.get("backend"),
                file_cfg.get("controller"),
                file_cfg.get("router_type"),
                file_cfg.get("firewall_backend"),
                DEFAULTS.router_type,
            )
        ),
    )

    controller_host = _first(
        args.host,
        _env_value(env, "STOPLIGA_CONTROLLER_HOST"),
        file_cfg.get("controller_host"),
    )
    controller_port = _first(
        args.port,
        _env_value(env, "STOPLIGA_CONTROLLER_PORT"),
        file_cfg.get("controller_port"),
    )
    controller_site = _first(
        args.site,
        _env_value(env, "STOPLIGA_SITE"),
        file_cfg.get("controller_site"),
    )
    controller_verify_tls = _first(
        args.unifi_verify_tls,
        _env_value(env, "STOPLIGA_CONTROLLER_VERIFY_TLS"),
        file_cfg.get("controller_verify_tls"),
    )
    controller_ca_file = _first(
        args.unifi_ca_file,
        _env_value(env, "STOPLIGA_CONTROLLER_CA_FILE"),
        file_cfg.get("controller_ca_file"),
    )

    unifi_host = _first(
        args.host,
        controller_host,
        _env_value(env, "UNIFI_HOST"),
        file_cfg.get("unifi_host"),
        DEFAULTS.host,
    )
    unifi_port = _first(
        args.port,
        controller_port,
        _env_value(env, "UNIFI_PORT"),
        file_cfg.get("unifi_port"),
        DEFAULTS.port,
    )
    unifi_site = _first(
        args.site,
        controller_site,
        _env_value(env, "UNIFI_SITE"),
        file_cfg.get("unifi_site"),
        DEFAULTS.site,
    )
    unifi_verify_tls = _first(
        args.unifi_verify_tls,
        controller_verify_tls,
        _env_value(env, "UNIFI_VERIFY_TLS"),
        file_cfg.get("unifi_verify_tls"),
        DEFAULTS.unifi_verify_tls,
    )
    unifi_ca_file = _first(
        args.unifi_ca_file,
        controller_ca_file,
        _env_value(env, "UNIFI_CA_FILE"),
        file_cfg.get("unifi_ca_file"),
    )

    omada_controller_host = _first(
        controller_host,
        _env_value(env, "OMADA_HOST"),
        file_cfg.get("omada_host"),
    )
    omada_controller_port = _first(
        controller_port,
        _env_value(env, "OMADA_PORT"),
        file_cfg.get("omada_port"),
        8043,
    )
    omada_base_url_raw = _first(
        args.omada_base_url,
        _env_value(env, "OMADA_BASE_URL"),
        _env_value(env, "STOPLIGA_OMADA_BASE_URL"),
        file_cfg.get("omada_base_url"),
    )
    if omada_base_url_raw is None and router_type == "omada" and omada_controller_host is not None:
        omada_base_url_raw = _build_controller_base_url(
            str(omada_controller_host),
            _parse_int(omada_controller_port, field_name="omada_port"),
        )
    omada_site = _first(
        args.site,
        controller_site,
        _env_value(env, "OMADA_SITE"),
        _env_value(env, "STOPLIGA_OMADA_SITE"),
        file_cfg.get("omada_site"),
        DEFAULTS.site,
    )
    omada_verify_tls = _first(
        args.omada_verify_tls,
        controller_verify_tls,
        _env_value(env, "OMADA_VERIFY_TLS"),
        _env_value(env, "STOPLIGA_OMADA_VERIFY_TLS"),
        file_cfg.get("omada_verify_tls"),
        DEFAULTS.omada_verify_tls,
    )
    omada_ca_file = _first(
        args.omada_ca_file,
        controller_ca_file,
        _env_value(env, "OMADA_CA_FILE"),
        _env_value(env, "STOPLIGA_OMADA_CA_FILE"),
        file_cfg.get("omada_ca_file"),
    )
    keenetic_controller_host = _first(
        controller_host,
        _env_value(env, "KEENETIC_HOST"),
        _env_value(env, "STOPLIGA_KEENETIC_HOST"),
    )
    keenetic_controller_port = _first(
        controller_port,
        _env_value(env, "KEENETIC_PORT"),
        _env_value(env, "STOPLIGA_KEENETIC_PORT"),
        80,
    )
    keenetic_base_url_raw = _first(
        args.keenetic_base_url,
        _env_value(env, "KEENETIC_BASE_URL"),
        _env_value(env, "STOPLIGA_KEENETIC_BASE_URL"),
        file_cfg.get("keenetic_base_url"),
    )
    if keenetic_base_url_raw is None and router_type == "keenetic" and keenetic_controller_host is not None:
        port_value = _parse_int(keenetic_controller_port, field_name="keenetic_port")
        base_url = f"http://{keenetic_controller_host}"
        if port_value != 80:
            base_url = f"{base_url}:{port_value}"
        keenetic_base_url_raw = base_url
    keenetic_verify_tls = _first(
        _env_value(env, "KEENETIC_VERIFY_TLS"),
        _env_value(env, "STOPLIGA_KEENETIC_VERIFY_TLS"),
        file_cfg.get("keenetic_verify_tls"),
        DEFAULTS.keenetic_verify_tls,
    )
    keenetic_ca_file = _first(
        args.keenetic_ca_file,
        _env_value(env, "KEENETIC_CA_FILE"),
        _env_value(env, "STOPLIGA_KEENETIC_CA_FILE"),
        file_cfg.get("keenetic_ca_file"),
    )

    log_level = (
        "DEBUG"
        if args.verbose
        else _first(
            args.log_level,
            _env_value(env, "STOPLIGA_LOG_LEVEL"),
            file_cfg.get("log_level"),
            DEFAULTS.log_level,
        )
    )

    config = Config(
        run_mode=cast(RunMode, _normalize_run_mode(args, env, file_cfg)),
        router_type=router_type,
        firewall_backend=cast(
            LegacyFirewallBackend | None,
            _first(
                _env_value(env, "STOPLIGA_FIREWALL_BACKEND"),
                file_cfg.get("firewall_backend"),
                DEFAULTS.firewall_backend,
            ),
        ),
        host=cast(str | None, unifi_host),
        port=_parse_int(unifi_port, field_name="port"),
        api_key=_first(
            args.api_key,
            _env_secret_first(
                env,
                field_name="api_key",
                key="UNIFI_API_KEY",
                key_file="UNIFI_API_KEY_FILE",
            ),
            file_cfg.get("api_key"),
            DEFAULTS.api_key,
        ),
        site=str(omada_site if router_type == "omada" else unifi_site),
        route_name=str(
            _first(
                args.route_name, _env_value(env, "STOPLIGA_ROUTE_NAME"), file_cfg.get("route_name"), DEFAULTS.route_name
            )
        ),
        destination_field=_normalize_destination_field(
            _first(
                args.destination_field,
                _env_value(env, "STOPLIGA_DESTINATION_FIELD"),
                file_cfg.get("destination_field"),
                DEFAULTS.destination_field,
            )
        ),
        omada_base_url=_normalize_omada_base_url(_first(omada_base_url_raw, DEFAULTS.omada_base_url)),
        omada_client_id=_first(
            args.omada_client_id,
            _env_value(env, "OMADA_CLIENT_ID"),
            _env_value(env, "STOPLIGA_OMADA_CLIENT_ID"),
            file_cfg.get("omada_client_id"),
            DEFAULTS.omada_client_id,
        ),
        omada_client_secret=_first(
            args.omada_client_secret,
            _env_secret_first(
                env,
                field_name="omada_client_secret",
                key="OMADA_CLIENT_SECRET",
                key_file="OMADA_CLIENT_SECRET_FILE",
            ),
            _env_secret_first(
                env,
                field_name="omada_client_secret",
                key="STOPLIGA_OMADA_CLIENT_SECRET",
                key_file="STOPLIGA_OMADA_CLIENT_SECRET_FILE",
            ),
            file_cfg.get("omada_client_secret"),
            DEFAULTS.omada_client_secret,
        ),
        omada_omadac_id=_first(
            args.omada_omadac_id,
            _env_value(env, "OMADA_CONTROLLER_ID"),
            _env_value(env, "OMADA_OMADAC_ID"),
            _env_value(env, "STOPLIGA_OMADA_OMADAC_ID"),
            file_cfg.get("omada_omadac_id"),
            DEFAULTS.omada_omadac_id,
        ),
        omada_target_type=cast(
            OmadaTargetType | None,
            _first(
                args.omada_target_type,
                _env_value(env, "OMADA_TARGET_TYPE"),
                _env_value(env, "STOPLIGA_OMADA_TARGET_TYPE"),
                file_cfg.get("omada_target_type"),
                DEFAULTS.omada_target_type,
            ),
        ),
        omada_target=_first(
            args.omada_target,
            _env_value(env, "OMADA_TARGET"),
            _env_value(env, "STOPLIGA_OMADA_TARGET"),
            file_cfg.get("omada_target"),
            DEFAULTS.omada_target,
        ),
        omada_source_networks=_parse_csv_list(
            _first(
                args.omada_source_networks,
                _env_value(env, "OMADA_SOURCE_NETWORKS"),
                _env_value(env, "STOPLIGA_OMADA_SOURCE_NETWORKS"),
                file_cfg.get("omada_source_networks"),
                DEFAULTS.omada_source_networks,
            ),
            field_name="omada_source_networks",
        ),
        omada_verify_tls=_parse_bool(
            omada_verify_tls,
            field_name="omada_verify_tls",
        ),
        omada_ca_file=_parse_path(value, field_name="omada_ca_file") if (value := omada_ca_file) else None,
        omada_group_size=_parse_int(
            _first(
                args.omada_group_size,
                _env_value(env, "OMADA_GROUP_SIZE"),
                _env_value(env, "STOPLIGA_OMADA_GROUP_SIZE"),
                file_cfg.get("omada_group_size"),
                DEFAULTS.omada_group_size,
            ),
            field_name="omada_group_size",
        ),
        keenetic_base_url=_first(keenetic_base_url_raw, DEFAULTS.keenetic_base_url),
        keenetic_username=_first(
            args.keenetic_username,
            _env_value(env, "KEENETIC_USERNAME"),
            _env_value(env, "STOPLIGA_KEENETIC_USERNAME"),
            file_cfg.get("keenetic_username"),
            DEFAULTS.keenetic_username,
        ),
        keenetic_password=_first(
            args.keenetic_password,
            _env_secret_first(
                env,
                field_name="keenetic_password",
                key="KEENETIC_PASSWORD",
                key_file="KEENETIC_PASSWORD_FILE",
            ),
            _env_secret_first(
                env,
                field_name="keenetic_password",
                key="STOPLIGA_KEENETIC_PASSWORD",
                key_file="STOPLIGA_KEENETIC_PASSWORD_FILE",
            ),
            file_cfg.get("keenetic_password"),
            DEFAULTS.keenetic_password,
        ),
        keenetic_verify_tls=_parse_bool(keenetic_verify_tls, field_name="keenetic_verify_tls"),
        keenetic_ca_file=_parse_path(value, field_name="keenetic_ca_file") if (value := keenetic_ca_file) else None,
        keenetic_interface=_first(
            args.keenetic_interface,
            _env_value(env, "KEENETIC_INTERFACE"),
            _env_value(env, "STOPLIGA_KEENETIC_INTERFACE"),
            file_cfg.get("keenetic_interface"),
            DEFAULTS.keenetic_interface,
        ),
        keenetic_gateway=_first(
            args.keenetic_gateway,
            _env_value(env, "KEENETIC_GATEWAY"),
            _env_value(env, "STOPLIGA_KEENETIC_GATEWAY"),
            file_cfg.get("keenetic_gateway"),
            DEFAULTS.keenetic_gateway,
        ),
        keenetic_auto=_parse_bool(
            _first(
                _env_value(env, "KEENETIC_AUTO"),
                _env_value(env, "STOPLIGA_KEENETIC_AUTO"),
                file_cfg.get("keenetic_auto"),
                DEFAULTS.keenetic_auto,
            ),
            field_name="keenetic_auto",
        ),
        keenetic_reject=_parse_bool(
            _first(
                _env_value(env, "KEENETIC_REJECT"),
                _env_value(env, "STOPLIGA_KEENETIC_REJECT"),
                file_cfg.get("keenetic_reject"),
                DEFAULTS.keenetic_reject,
            ),
            field_name="keenetic_reject",
        ),
        status_url=str(
            _first(
                args.status_url, _env_value(env, "STOPLIGA_STATUS_URL"), file_cfg.get("status_url"), DEFAULTS.status_url
            )
        ),
        ip_list_url=str(
            _first(
                args.ip_list_url,
                _env_value(env, "STOPLIGA_IP_LIST_URL"),
                file_cfg.get("ip_list_url"),
                DEFAULTS.ip_list_url,
            )
        ),
        unifi_verify_tls=_parse_bool(
            unifi_verify_tls,
            field_name="unifi_verify_tls",
        ),
        unifi_ca_file=_parse_path(value, field_name="unifi_ca_file") if (value := unifi_ca_file) else None,
        opnsense_host=_first(_env_value(env, "OPNSENSE_HOST"), file_cfg.get("opnsense_host"), DEFAULTS.opnsense_host),
        opnsense_api_key=_first(
            _env_secret_first(
                env,
                field_name="opnsense_api_key",
                key="OPNSENSE_API_KEY",
                key_file="OPNSENSE_API_KEY_FILE",
            ),
            file_cfg.get("opnsense_api_key"),
            DEFAULTS.opnsense_api_key,
        ),
        opnsense_api_secret=_first(
            _env_secret_first(
                env,
                field_name="opnsense_api_secret",
                key="OPNSENSE_API_SECRET",
                key_file="OPNSENSE_API_SECRET_FILE",
            ),
            file_cfg.get("opnsense_api_secret"),
            DEFAULTS.opnsense_api_secret,
        ),
        opnsense_verify_tls=_parse_bool(
            _first(
                _env_value(env, "OPNSENSE_VERIFY_TLS"),
                file_cfg.get("opnsense_verify_tls"),
                DEFAULTS.opnsense_verify_tls,
            ),
            field_name="opnsense_verify_tls",
        ),
        opnsense_ca_file=_parse_path(value, field_name="opnsense_ca_file")
        if (value := _first(_env_value(env, "OPNSENSE_CA_FILE"), file_cfg.get("opnsense_ca_file")))
        else None,
        opnsense_alias_name=_first(
            _env_value(env, "OPNSENSE_ALIAS_NAME"),
            file_cfg.get("opnsense_alias_name"),
            DEFAULTS.opnsense_alias_name,
        ),
        feed_verify_tls=_parse_bool(
            _first(
                _env_value(env, "STOPLIGA_FEED_VERIFY_TLS"), file_cfg.get("feed_verify_tls"), DEFAULTS.feed_verify_tls
            ),
            field_name="feed_verify_tls",
        ),
        feed_ca_file=_parse_path(value, field_name="feed_ca_file")
        if (value := _first(_env_value(env, "STOPLIGA_FEED_CA_FILE"), file_cfg.get("feed_ca_file")))
        else None,
        feed_allow_private_hosts=_parse_bool(
            _first(
                _env_value(env, "STOPLIGA_FEED_ALLOW_PRIVATE_HOSTS"),
                file_cfg.get("feed_allow_private_hosts"),
                DEFAULTS.feed_allow_private_hosts,
            ),
            field_name="feed_allow_private_hosts",
        ),
        strict_feed_consistency=_parse_bool(
            _first(
                _env_value(env, "STOPLIGA_STRICT_FEED_CONSISTENCY"),
                file_cfg.get("strict_feed_consistency"),
                DEFAULTS.strict_feed_consistency,
            ),
            field_name="strict_feed_consistency",
        ),
        request_timeout=_parse_float(
            _first(
                args.request_timeout,
                _env_value(env, "STOPLIGA_REQUEST_TIMEOUT"),
                file_cfg.get("request_timeout"),
                DEFAULTS.request_timeout,
            ),
            field_name="request_timeout",
        ),
        retries=_parse_int(
            _first(args.retries, _env_value(env, "STOPLIGA_RETRIES"), file_cfg.get("retries"), DEFAULTS.retries),
            field_name="retries",
        ),
        max_response_bytes=_parse_int(
            _first(
                args.max_response_bytes,
                _env_value(env, "STOPLIGA_MAX_RESPONSE_BYTES"),
                file_cfg.get("max_response_bytes"),
                DEFAULTS.max_response_bytes,
            ),
            field_name="max_response_bytes",
        ),
        interval_seconds=_parse_int(
            _first(
                args.interval_seconds,
                _env_value(env, "STOPLIGA_SYNC_INTERVAL_SECONDS"),
                file_cfg.get("interval_seconds"),
                DEFAULTS.interval_seconds,
            ),
            field_name="interval_seconds",
        ),
        dry_run=_parse_bool(
            _first(args.dry_run, _env_value(env, "STOPLIGA_DRY_RUN"), file_cfg.get("dry_run"), DEFAULTS.dry_run),
            field_name="dry_run",
        ),
        invalid_entry_policy=cast(
            InvalidEntryPolicy,
            str(
                _first(
                    args.invalid_entry_policy,
                    _env_value(env, "STOPLIGA_INVALID_ENTRY_POLICY"),
                    file_cfg.get("invalid_entry_policy"),
                    DEFAULTS.invalid_entry_policy,
                )
            ),
        ),
        max_destinations=_parse_int(
            _first(
                args.max_destinations,
                _env_value(env, "STOPLIGA_MAX_DESTINATIONS"),
                file_cfg.get("max_destinations"),
                DEFAULTS.max_destinations,
            ),
            field_name="max_destinations",
        ),
        state_file=_parse_path(
            _first(
                args.state_file,
                _env_value(env, "STOPLIGA_STATE_FILE"),
                file_cfg.get("state_file"),
                str(DEFAULTS.state_file),
            ),
            field_name="state_file",
        ),
        lock_file=_parse_path(
            _first(
                args.lock_file,
                _env_value(env, "STOPLIGA_LOCK_FILE"),
                file_cfg.get("lock_file"),
                str(DEFAULTS.lock_file),
            ),
            field_name="lock_file",
        ),
        bootstrap_guard_file=_parse_path(
            _first(
                _env_value(env, "STOPLIGA_BOOTSTRAP_GUARD_FILE"),
                file_cfg.get("bootstrap_guard_file"),
                str(DEFAULTS.bootstrap_guard_file),
            ),
            field_name="bootstrap_guard_file",
        ),
        health_max_age_seconds=_parse_int(value, field_name="health_max_age_seconds")
        if (
            value := _first(
                args.health_max_age_seconds,
                _env_value(env, "STOPLIGA_HEALTH_MAX_AGE_SECONDS"),
                file_cfg.get("health_max_age_seconds"),
            )
        )
        is not None
        else None,
        log_level=_validate_log_level(str(log_level)),
        vpn_name=_first(
            args.vpn_name, _env_value(env, "STOPLIGA_VPN_NAME"), file_cfg.get("vpn_name"), DEFAULTS.vpn_name
        ),
        target_clients=_parse_csv_list(
            _first(
                args.targets,
                _env_value(env, "STOPLIGA_TARGETS"),
                file_cfg.get("target_clients"),
                DEFAULTS.target_clients,
            ),
            field_name="target_clients",
        ),
        dump_payloads_on_error=_parse_bool(
            _first(
                args.dump_payloads_on_error,
                _env_value(env, "STOPLIGA_DUMP_PAYLOADS_ON_ERROR"),
                file_cfg.get("dump_payloads_on_error"),
                DEFAULTS.dump_payloads_on_error,
            ),
            field_name="dump_payloads_on_error",
        ),
        gotify_url=_first(
            args.gotify_url, _env_value(env, "STOPLIGA_GOTIFY_URL"), file_cfg.get("gotify_url"), DEFAULTS.gotify_url
        ),
        gotify_token=_first(
            args.gotify_token,
            _env_secret_first(
                env, field_name="gotify_token", key="STOPLIGA_GOTIFY_TOKEN", key_file="STOPLIGA_GOTIFY_TOKEN_FILE"
            ),
            file_cfg.get("gotify_token"),
            DEFAULTS.gotify_token,
        ),
        gotify_priority=_parse_int(
            _first(
                args.gotify_priority,
                _env_value(env, "STOPLIGA_GOTIFY_PRIORITY"),
                file_cfg.get("gotify_priority"),
                DEFAULTS.gotify_priority,
            ),
            field_name="gotify_priority",
        ),
        telegram_bot_token=_first(
            args.telegram_bot_token,
            _env_secret_first(
                env,
                field_name="telegram_bot_token",
                key="STOPLIGA_TELEGRAM_BOT_TOKEN",
                key_file="STOPLIGA_TELEGRAM_BOT_TOKEN_FILE",
            ),
            file_cfg.get("telegram_bot_token"),
            DEFAULTS.telegram_bot_token,
        ),
        telegram_chat_id=str(
            _first(
                args.telegram_chat_id,
                _env_value(env, "STOPLIGA_TELEGRAM_CHAT_ID"),
                file_cfg.get("telegram_chat_id"),
                DEFAULTS.telegram_chat_id,
            )
        )
        if _first(
            args.telegram_chat_id,
            _env_value(env, "STOPLIGA_TELEGRAM_CHAT_ID"),
            file_cfg.get("telegram_chat_id"),
            DEFAULTS.telegram_chat_id,
        )
        is not None
        else None,
        telegram_group_id=str(
            _first(
                args.telegram_group_id,
                _env_value(env, "STOPLIGA_TELEGRAM_GROUP_ID"),
                file_cfg.get("telegram_group_id"),
                DEFAULTS.telegram_group_id,
            )
        )
        if _first(
            args.telegram_group_id,
            _env_value(env, "STOPLIGA_TELEGRAM_GROUP_ID"),
            file_cfg.get("telegram_group_id"),
            DEFAULTS.telegram_group_id,
        )
        is not None
        else None,
        telegram_topic_id=_parse_int(
            _first(
                args.telegram_topic_id, _env_value(env, "STOPLIGA_TELEGRAM_TOPIC_ID"), file_cfg.get("telegram_topic_id")
            ),
            field_name="telegram_topic_id",
        )
        if _first(
            args.telegram_topic_id, _env_value(env, "STOPLIGA_TELEGRAM_TOPIC_ID"), file_cfg.get("telegram_topic_id")
        )
        is not None
        else None,
        notification_timeout=_parse_float(
            _first(
                args.notification_timeout,
                _env_value(env, "STOPLIGA_NOTIFICATION_TIMEOUT"),
                file_cfg.get("notification_timeout"),
                DEFAULTS.notification_timeout,
            ),
            field_name="notification_timeout",
        ),
        notification_retries=_parse_int(
            _first(
                args.notification_retries,
                _env_value(env, "STOPLIGA_NOTIFICATION_RETRIES"),
                file_cfg.get("notification_retries"),
                DEFAULTS.notification_retries,
            ),
            field_name="notification_retries",
        ),
        notification_verify_tls=_parse_bool(
            _first(
                _env_value(env, "STOPLIGA_NOTIFICATION_VERIFY_TLS"),
                file_cfg.get("notification_verify_tls"),
                DEFAULTS.notification_verify_tls,
            ),
            field_name="notification_verify_tls",
        ),
        notification_ca_file=_parse_path(value, field_name="notification_ca_file")
        if (value := _first(_env_value(env, "STOPLIGA_NOTIFICATION_CA_FILE"), file_cfg.get("notification_ca_file")))
        else None,
        gotify_verify_tls=(
            _parse_bool(value, field_name="gotify_verify_tls")
            if (value := _first(_env_value(env, "STOPLIGA_GOTIFY_VERIFY_TLS"), file_cfg.get("gotify_verify_tls")))
            is not None
            else None
        ),
        gotify_ca_file=_parse_path(value, field_name="gotify_ca_file")
        if (value := _first(_env_value(env, "STOPLIGA_GOTIFY_CA_FILE"), file_cfg.get("gotify_ca_file")))
        else None,
        gotify_allow_plain_http=_parse_bool(
            _first(
                _env_value(env, "STOPLIGA_GOTIFY_ALLOW_PLAIN_HTTP"),
                file_cfg.get("gotify_allow_plain_http"),
                DEFAULTS.gotify_allow_plain_http,
            ),
            field_name="gotify_allow_plain_http",
        ),
        telegram_verify_tls=(
            _parse_bool(value, field_name="telegram_verify_tls")
            if (value := _first(_env_value(env, "STOPLIGA_TELEGRAM_VERIFY_TLS"), file_cfg.get("telegram_verify_tls")))
            is not None
            else None
        ),
        telegram_ca_file=_parse_path(value, field_name="telegram_ca_file")
        if (value := _first(_env_value(env, "STOPLIGA_TELEGRAM_CA_FILE"), file_cfg.get("telegram_ca_file")))
        else None,
    )

    validate_config(config, validate_connection=validate and not args.healthcheck)
    return config


def validate_config(config: Config, *, validate_connection: bool) -> None:
    """Validate configuration invariants."""

    if config.retries < 1:
        raise ConfigError("retries must be >= 1")
    if config.request_timeout <= 0:
        raise ConfigError("request_timeout must be > 0")
    if config.max_response_bytes < 1024:
        raise ConfigError("max_response_bytes must be >= 1024")
    if config.notification_timeout <= 0:
        raise ConfigError("notification_timeout must be > 0")
    if config.interval_seconds <= 0 and config.run_mode == "loop":
        raise ConfigError("loop mode requires interval_seconds > 0")
    if config.health_max_age_seconds is not None and config.health_max_age_seconds <= 0:
        raise ConfigError("health_max_age_seconds must be > 0 when set")
    if config.max_destinations < 1:
        raise ConfigError("max_destinations must be >= 1")
    if config.omada_group_size < 1:
        raise ConfigError("omada_group_size must be >= 1")
    if config.notification_retries < 1:
        raise ConfigError("notification_retries must be >= 1")
    if config.log_level not in VALID_LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(sorted(VALID_LOG_LEVELS))}, not {config.log_level!r}")
    if not config.route_name.strip():
        raise ConfigError("route_name must not be empty")
    if not config.site.strip():
        raise ConfigError("site must not be empty")
    if len({config.state_file, config.lock_file, config.bootstrap_guard_file}) != 3:
        raise ConfigError("state_file, lock_file and bootstrap_guard_file must be different paths")
    if config.invalid_entry_policy not in {"fail", "ignore"}:
        raise ConfigError(f"invalid_entry_policy must be fail|ignore, not {config.invalid_entry_policy!r}")
    if config.router_type not in get_args(RouterType):
        raise ConfigError(f"router_type must be one of {', '.join(get_args(RouterType))}, not {config.router_type!r}")
    if bool(config.vpn_name) != bool(config.target_clients):
        raise ConfigError("Automatic route creation requires both STOPLIGA_VPN_NAME and STOPLIGA_TARGETS")
    if config.router_type != "unifi" and (config.vpn_name or config.target_clients):
        raise ConfigError("STOPLIGA_VPN_NAME and STOPLIGA_TARGETS are UniFi-only bootstrap options")
    if bool(config.gotify_url) != bool(config.gotify_token):
        raise ConfigError("Gotify notifications require both STOPLIGA_GOTIFY_URL and STOPLIGA_GOTIFY_TOKEN")
    if config.telegram_chat_id and config.telegram_group_id:
        raise ConfigError("Set either STOPLIGA_TELEGRAM_CHAT_ID or STOPLIGA_TELEGRAM_GROUP_ID, not both")
    telegram_target = config.resolved_telegram_chat_id()
    if bool(config.telegram_bot_token) != bool(telegram_target):
        raise ConfigError(
            "Telegram notifications require STOPLIGA_TELEGRAM_BOT_TOKEN and either STOPLIGA_TELEGRAM_CHAT_ID or STOPLIGA_TELEGRAM_GROUP_ID"
        )
    if config.telegram_topic_id is not None and not telegram_target:
        raise ConfigError("STOPLIGA_TELEGRAM_TOPIC_ID requires STOPLIGA_TELEGRAM_GROUP_ID or STOPLIGA_TELEGRAM_CHAT_ID")
    if config.telegram_topic_id is not None and config.telegram_topic_id <= 0:
        raise ConfigError("STOPLIGA_TELEGRAM_TOPIC_ID must be > 0")
    if config.router_type == "omada":
        if len(config.route_name.strip()) > 64:
            raise ConfigError("omada route_name must be 64 characters or fewer")
        if validate_connection:
            if config.omada_target_type not in get_args(OmadaTargetType):
                raise ConfigError(
                    f"omada_target_type must be one of {', '.join(get_args(OmadaTargetType))}, not {config.omada_target_type!r}"
                )
            if not config.omada_target or not config.omada_target.strip():
                raise ConfigError("omada mode requires STOPLIGA_OMADA_TARGET")
            _validate_api_base_url(config.omada_base_url or "", field_name="omada_base_url")
    elif config.router_type == "keenetic":
        if validate_connection:
            _validate_api_base_url(config.keenetic_base_url or "", field_name="keenetic_base_url")
        if not config.keenetic_interface or not config.keenetic_interface.strip():
            raise ConfigError("keenetic mode requires KEENETIC_INTERFACE or STOPLIGA_KEENETIC_INTERFACE")
        if config.keenetic_reject and not config.keenetic_auto:
            raise ConfigError("keenetic_reject requires keenetic_auto=true")
    elif validate_connection and config.router_type == "opnsense":
        _validate_host(config.opnsense_host or "", field_name="OPNSENSE_HOST")
    elif validate_connection:
        _validate_host(config.host or "", field_name="UNIFI_HOST")
    if config.gotify_url:
        _validate_gotify_url(config.gotify_url, allow_plain_http=config.gotify_allow_plain_http)
    if config.telegram_bot_token:
        if config.telegram_verify_tls is False:
            raise ConfigError("telegram_verify_tls=false is not supported; Telegram notifications must verify TLS")
    _validate_feed_url(
        config.status_url,
        field_name="status_url",
        allow_private_hosts=config.feed_allow_private_hosts,
        allow_dns=True,
    )
    _validate_feed_url(
        config.ip_list_url,
        field_name="ip_list_url",
        allow_private_hosts=config.feed_allow_private_hosts,
    )
    if validate_connection:
        if not config.has_router_api_access():
            if config.router_type == "omada":
                raise ConfigError(
                    "omada mode requires STOPLIGA_OMADA_BASE_URL or STOPLIGA_CONTROLLER_HOST, "
                    "OMADA_CLIENT_ID/STOPLIGA_OMADA_CLIENT_ID, "
                    "OMADA_CLIENT_SECRET/STOPLIGA_OMADA_CLIENT_SECRET, "
                    "OMADA_CONTROLLER_ID/STOPLIGA_OMADA_OMADAC_ID, "
                    "OMADA_TARGET_TYPE/STOPLIGA_OMADA_TARGET_TYPE and "
                    "OMADA_TARGET/STOPLIGA_OMADA_TARGET"
                )
            if config.router_type == "opnsense":
                raise ConfigError("opnsense mode requires OPNSENSE_HOST, OPNSENSE_API_KEY and OPNSENSE_API_SECRET")
            if config.router_type == "keenetic":
                raise ConfigError(
                    "keenetic mode requires KEENETIC_BASE_URL (or STOPLIGA_CONTROLLER_HOST), "
                    "KEENETIC_USERNAME, KEENETIC_PASSWORD and KEENETIC_INTERFACE"
                )
            raise ConfigError("unifi mode requires STOPLIGA_CONTROLLER_HOST (or UNIFI_HOST) and UNIFI_API_KEY")
