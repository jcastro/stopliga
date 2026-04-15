"""Typed data models used across the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


RunMode = Literal["once", "loop"]
InvalidEntryPolicy = Literal["fail", "ignore"]


@dataclass(frozen=True)
class Config:
    run_mode: RunMode = "once"
    host: str | None = None
    port: int = 443
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    site: str = "default"
    route_name: str = "StopLiga"
    destination_field: str = "auto"
    status_url: str = (
        "https://raw.githubusercontent.com/r4y7s/laliga-ip-list/main/laliga_status.json"
    )
    ip_list_url: str = (
        "https://raw.githubusercontent.com/r4y7s/laliga-ip-list/main/laliga_ip_list.txt"
    )
    enable_when_blocked: bool = True
    unifi_verify_tls: bool = True
    unifi_ca_file: Path | None = None
    feed_verify_tls: bool = True
    feed_ca_file: Path | None = None
    request_timeout: float = 15.0
    retries: int = 4
    interval_seconds: int = 300
    dry_run: bool = False
    invalid_entry_policy: InvalidEntryPolicy = "fail"
    max_destinations: int = 2048
    state_file: Path = Path("/data/state.json")
    lock_file: Path = Path("/data/stopliga.lock")
    health_max_age_seconds: int | None = None
    log_level: str = "INFO"
    vpn_name: str | None = None
    target_clients: tuple[str, ...] = ()
    dump_payloads_on_error: bool = False

    def has_unifi_auth(self) -> bool:
        return bool(self.host and ((self.api_key and self.api_key.strip()) or (self.username and self.password)))

    def resolved_health_max_age(self) -> int:
        if self.health_max_age_seconds is not None and self.health_max_age_seconds > 0:
            return self.health_max_age_seconds
        if self.run_mode == "loop":
            return max(300, self.interval_seconds * 3)
        return 86400


@dataclass(frozen=True)
class FeedSnapshot:
    is_blocked: bool
    desired_enabled: bool
    destinations: list[str]
    raw_status: dict[str, Any]
    raw_line_count: int
    valid_count: int
    invalid_count: int
    invalid_entries: list[str]
    destinations_hash: str
    feed_hash: str


@dataclass
class SiteContext:
    internal_name: str
    site_id: str | None = None
    network_record: dict[str, Any] | None = None
    official_record: dict[str, Any] | None = None


@dataclass
class UpdatePlan:
    backend_name: str
    route_id: str
    route_label: str
    route_endpoint: str
    route_method: str
    current_enabled: bool | None
    desired_enabled: bool
    current_destinations: list[str]
    desired_destinations: list[str]
    route_payload: dict[str, Any] | None = None
    route_changed_fields: list[str] = field(default_factory=list)
    linked_list_id: str | None = None
    linked_list_endpoint: str | None = None
    linked_list_payload: dict[str, Any] | None = None
    linked_list_changed_fields: list[str] = field(default_factory=list)
    linked_list_current_destinations: list[str] = field(default_factory=list)
    raw_route: dict[str, Any] | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.route_changed_fields or self.linked_list_changed_fields)


@dataclass(frozen=True)
class BootstrapPreview:
    backend_name: str
    payload: dict[str, Any]
    source: str


@dataclass(frozen=True)
class SyncResult:
    mode: str
    route_name: str
    route_id: str | None
    backend_name: str | None
    changed: bool
    created: bool
    dry_run: bool
    desired_enabled: bool
    current_enabled: bool | None
    desired_destinations: int
    current_destinations: int
    invalid_entries: int
    feed_hash: str
    destinations_hash: str
    summary: str
    bootstrap_source: str | None = None
    bootstrap_network_id: str | None = None
    bootstrap_target_macs: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateSnapshot:
    status: str
    run_mode: str
    route_name: str
    site: str
    last_attempt_at: str
    last_success_at: str | None
    last_error: str | None
    last_mode: str | None
    last_route_id: str | None
    last_backend: str | None
    feed_hash: str | None
    destinations_hash: str | None
    changed: bool
    created: bool
    dry_run: bool
    partial_failure: bool = False
    last_error_stage: str | None = None
    bootstrap_source: str | None = None
    bootstrap_network_id: str | None = None
    bootstrap_target_macs: tuple[str, ...] = ()
