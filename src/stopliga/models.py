"""Typed data models used across the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


RunMode = Literal["once", "loop"]
InvalidEntryPolicy = Literal["fail", "ignore"]
FirewallBackend = Literal["unifi", "opnsense"]


@dataclass(frozen=True)
class Config:
    run_mode: RunMode = "once"
    firewall_backend: FirewallBackend = "unifi"
    host: str | None = None
    port: int = 443
    api_key: str | None = None
    site: str = "default"
    route_name: str = "StopLiga"
    destination_field: str = "auto"
    status_url: str = (
        "https://raw.githubusercontent.com/r4y7s/laliga-ip-list/main/laliga_status.json"
    )
    ip_list_url: str = (
        "https://raw.githubusercontent.com/r4y7s/laliga-ip-list/main/laliga_ip_list.txt"
    )
    unifi_verify_tls: bool = True
    unifi_ca_file: Path | None = None
    opnsense_host: str | None = None
    opnsense_api_key: str | None = None
    opnsense_api_secret: str | None = None
    opnsense_verify_tls: bool = True
    opnsense_ca_file: Path | None = None
    opnsense_alias_name: str | None = None
    feed_verify_tls: bool = True
    feed_ca_file: Path | None = None
    feed_allow_private_hosts: bool = False
    strict_feed_consistency: bool = True
    request_timeout: float = 15.0
    retries: int = 4
    max_response_bytes: int = 2 * 1024 * 1024
    interval_seconds: int = 300
    dry_run: bool = False
    invalid_entry_policy: InvalidEntryPolicy = "fail"
    max_destinations: int = 2048
    state_file: Path = Path("/data/state.json")
    lock_file: Path = Path("/data/stopliga.lock")
    bootstrap_guard_file: Path = Path("/data/bootstrap_guard.json")
    health_max_age_seconds: int | None = None
    log_level: str = "INFO"
    vpn_name: str | None = None
    target_clients: tuple[str, ...] = ()
    dump_payloads_on_error: bool = False
    gotify_url: str | None = None
    gotify_token: str | None = None
    gotify_priority: int = 5
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_group_id: str | None = None
    telegram_topic_id: int | None = None
    notification_timeout: float = 10.0
    notification_retries: int = 2
    notification_verify_tls: bool = True
    notification_ca_file: Path | None = None
    gotify_verify_tls: bool | None = None
    gotify_ca_file: Path | None = None
    gotify_allow_plain_http: bool = False
    telegram_verify_tls: bool | None = None
    telegram_ca_file: Path | None = None

    def has_local_api_access(self) -> bool:
        if self.firewall_backend == "opnsense":
            return bool(self.opnsense_host and self.opnsense_api_key and self.opnsense_api_secret)
        return bool(self.host and self.api_key and self.api_key.strip())

    def has_notifications(self) -> bool:
        return bool(
            (self.gotify_url and self.gotify_token)
            or (self.telegram_bot_token and self.resolved_telegram_chat_id())
        )

    def resolved_telegram_chat_id(self) -> str | None:
        return self.telegram_group_id or self.telegram_chat_id

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
    is_blocked: bool = False
    added_destinations: int = 0
    removed_destinations: int = 0
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
    last_sync_id: str | None
    last_route_id: str | None
    last_backend: str | None
    feed_hash: str | None
    destinations_hash: str | None
    changed: bool
    created: bool
    dry_run: bool
    consecutive_failures: int = 0
    partial_failure: bool = False
    last_error_stage: str | None = None
    rollback_attempted: bool = False
    rollback_completed: bool = False
    rollback_error: str | None = None
    reconciliation_required: bool = False
    last_is_blocked: bool | None = None
    bootstrap_source: str | None = None
    bootstrap_network_id: str | None = None
    bootstrap_target_macs: tuple[str, ...] = ()
