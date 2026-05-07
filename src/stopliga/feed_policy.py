"""Shared feed URL safety policy."""

from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlparse

from .errors import ConfigError


LOOPBACK_HTTP_HOSTS = frozenset({"127.0.0.1", "localhost"})


def is_private_hostname(hostname: str) -> bool:
    lowered = hostname.strip().lower()
    if lowered in {"localhost"}:
        return True
    try:
        ip_value = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(ip_value.is_private or ip_value.is_loopback or ip_value.is_link_local or ip_value.is_reserved)


def validate_feed_url(
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
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HTTP_HOSTS:
        raise ConfigError(f"{field_name} only allows plain HTTP for localhost/127.0.0.1")
    if is_private_hostname(parsed.hostname) and not allow_private_hosts and parsed.hostname not in LOOPBACK_HTTP_HOSTS:
        raise ConfigError(f"{field_name} points to a private or local host; set feed_allow_private_hosts to override")


def validate_feed_redirect_url(
    redirect_url: str,
    *,
    source_url: str,
    allow_private_hosts: bool,
) -> None:
    resolved_url = urljoin(source_url, redirect_url)
    parsed = urlparse(resolved_url)
    source_host = urlparse(source_url).hostname
    source_is_explicit_loopback = source_host in LOOPBACK_HTTP_HOSTS

    if parsed.scheme not in {"https", "http"}:
        raise ConfigError(f"feed redirect target must use http or https, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigError("feed redirect target must include a hostname")
    if parsed.username or parsed.password:
        raise ConfigError("feed redirect target must not embed credentials")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HTTP_HOSTS:
        raise ConfigError("feed redirect target only allows plain HTTP for localhost/127.0.0.1")
    if is_private_hostname(parsed.hostname) and not allow_private_hosts:
        if not (source_is_explicit_loopback and parsed.hostname in LOOPBACK_HTTP_HOSTS):
            raise ConfigError(
                "feed redirect target points to a private or local host; set feed_allow_private_hosts to override"
            )
