"""Logging configuration helpers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import sys
from typing import Any


SENSITIVE_FIELD_MARKERS = {"password", "secret", "token", "api_key", "key"}
_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("stopliga_log_context", default={})


def _quote(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=True)


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        key_lower = key.lower()
        if any(marker in key_lower for marker in SENSITIVE_FIELD_MARKERS):
            sanitized[key] = "***"
        else:
            sanitized[key] = value
    return sanitized


def _event_message(event: str | None, fields: dict[str, Any]) -> str | None:
    if event == "loop_start":
        return "Loop mode started"
    if event == "loop_stop":
        return "Loop mode stopped"
    if event == "sync_start":
        return "Starting sync"
    if event == "feed_check":
        return "Checking blocking status and IP list"
    if event == "feed_canonical_status_fallback":
        return "Canonical Hayahora status feed unavailable, falling back to DNS"
    if event == "feed_revision_resolved":
        return "Pinned feed to a single GitHub revision"
    if event == "feed_loaded":
        return "Feed loaded: blocking active" if fields.get("is_blocked") else "Feed loaded: blocking inactive"
    if event == "feed_invalid_entries_ignored":
        return "Ignored invalid feed entries"
    if event == "network_prefix_detected":
        return "Detected UniFi Network API"
    if event == "site_resolved":
        return "Connected to UniFi site"
    if event == "route_found":
        return "Found managed route"
    if event == "route_bootstrap_prepared":
        return "Route not found, preparing bootstrap"
    if event == "route_bootstrap_retry":
        return "UniFi rejected ALL_CLIENTS, retrying bootstrap with a single detected client"
    if event == "vpn_client_network_missing":
        return "No UniFi VPN client network found"
    if event == "route_check":
        if fields.get("pending_manual_review"):
            return "Route needs manual review before activation"
        if fields.get("current_enabled") != fields.get("desired_enabled"):
            return "Route enabled state needs update"
        if fields.get("current_destinations") != fields.get("desired_destinations"):
            return "Route destination list needs update"
        return "Route already in sync"
    if event == "route_ip_delta":
        return "Route destination IP list changed"
    if event == "route_plan":
        if fields.get("dry_run"):
            return "Dry run completed"
        return "Applying route changes" if fields.get("changed") else "No route changes needed"
    if event == "route_updating":
        return "Updating route in UniFi"
    if event == "linked_list_updating":
        return "Updating linked IP list in UniFi"
    if event == "sync_finish":
        return "Sync finished"
    if event == "notification_sent":
        return "Notification sent"
    if event == "notification_failed":
        return "Notification failed"
    if event == "notification_provider_failed":
        return "Notification provider failed"
    if event == "reconciliation_pending":
        return "Sync blocked until manual reconciliation is done"
    if event == "rollback_attempt":
        return "Attempting rollback"
    if event == "rollback_completed":
        return "Rollback completed"
    if event == "rollback_failed":
        return "Rollback failed"
    if event == "config_error":
        return "Configuration error"
    if event == "authentication_error":
        return "Authentication error"
    if event == "route_error":
        return "Route error"
    if event == "sync_error":
        return "Synchronization failed"
    if event == "state_error":
        return "State error"
    if event == "state_load_failed":
        return "Could not read runtime state"
    if event == "bootstrap_guard_load_failed":
        return "Could not read bootstrap guard"
    if event == "state_file_quarantined":
        return "Invalid runtime state file was quarantined"
    if event == "bootstrap_guard_quarantined":
        return "Invalid bootstrap guard file was quarantined"
    if event == "state_write_failed":
        return "Could not write runtime state"
    if event == "unsupported_route_shape":
        return "UniFi returned an unsupported route shape"
    if event == "healthcheck":
        return "Healthcheck"
    if event == "signal_received":
        return "Signal received"
    if event == "interrupted":
        return "Interrupted"
    return None


def _visible_fields(event: str | None, fields: dict[str, Any], levelno: int) -> dict[str, Any]:
    if levelno <= logging.DEBUG:
        return dict(fields)

    visible = dict(fields)

    # Sync IDs are useful for deep debugging but noisy in normal container logs.
    visible.pop("sync_id", None)

    suppressed_by_event: dict[str, set[str]] = {
        "feed_check": {"status_url", "ip_list_url", "strict_consistency"},
        "feed_revision_resolved": {"revision"},
        "feed_loaded": {"revision", "desired_enabled", "raw_lines", "feed_hash"},
        "network_prefix_detected": {"prefix"},
        "site_resolved": {"site_id"},
        "route_found": {"backend", "endpoint"},
        "route_check": {"backend", "route_id"},
        "route_ip_delta": {"route_id", "added_sample", "removed_sample"},
        "route_plan": {"route_id"},
        "route_updating": {"route_id", "backend", "method"},
        "linked_list_updating": {"linked_list_id"},
        "sync_finish": {"route_id"},
    }
    if event is not None:
        suppressed_fields = suppressed_by_event.get(event)
        if suppressed_fields:
            for key in suppressed_fields:
                visible.pop(key, None)

    return visible


class KeyValueFormatter(logging.Formatter):
    """Simple key=value formatter that stays readable in container logs."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        event = getattr(record, "event", None)
        merged_fields: dict[str, Any] = {}
        merged_fields.update(_LOG_CONTEXT.get({}))
        if isinstance(fields, dict):
            merged_fields.update(fields)
        sanitized = _sanitize_fields(merged_fields)
        message = _event_message(event, sanitized)
        raw_message = record.getMessage()
        if not message and raw_message and (event is None or raw_message != event):
            message = raw_message

        parts = [record.levelname]
        if message:
            parts.append(message)

        visible_fields = _visible_fields(event, sanitized, record.levelno)
        if record.levelno <= logging.DEBUG:
            visible_fields.setdefault("logger", record.name)
            if event:
                visible_fields.setdefault("event", event)
        if record.exc_info:
            visible_fields["exception"] = self.formatException(record.exc_info)

        parts.extend(f"{key}={_quote(value)}" for key, value in visible_fields.items())
        return " ".join(parts)


def configure_logging(level_name: str) -> None:
    """Configure application-wide logging."""

    formatter = KeyValueFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(lambda record: record.levelno >= logging.ERROR)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a structured log event."""

    logger.log(level, event, extra={"event": event, "fields": fields})


@contextmanager
def log_context(**fields: Any):
    """Temporarily attach structured fields to all logs in the current context."""

    current = dict(_LOG_CONTEXT.get({}))
    current.update(fields)
    token = _LOG_CONTEXT.set(current)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)
