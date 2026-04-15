"""Logging configuration helpers."""

from __future__ import annotations

import json
import logging
from typing import Any


SENSITIVE_FIELD_MARKERS = {"password", "secret", "token", "api_key", "key"}


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


class KeyValueFormatter(logging.Formatter):
    """Simple key=value formatter that stays readable in Docker stdout."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        event = getattr(record, "event", None)
        sanitized = _sanitize_fields(fields if isinstance(fields, dict) else {})
        payload = {
            "level": record.levelname,
            "logger": record.name,
        }
        if event:
            payload["event"] = event
        message = record.getMessage()
        if message and (event is None or message != event):
            payload["message"] = message
        payload.update(sanitized)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return " ".join(f"{key}={_quote(value)}" for key, value in payload.items())


def configure_logging(level_name: str) -> None:
    """Configure application-wide logging."""

    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    root.addHandler(handler)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a structured log event."""

    logger.log(level, event, extra={"event": event, "fields": fields})
