"""Notification delivery helpers."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .errors import NetworkError
from .logging_utils import log_event
from .models import Config, SyncResult
from .utils import make_ssl_context


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=make_ssl_context(verify=True)))
    try:
        with opener.open(request, timeout=timeout):
            return
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"Notification request failed for {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NetworkError(f"Notification request failed for {url}: {exc}") from exc


def build_notification_message(result: SyncResult, previous_state: dict[str, object]) -> str | None:
    lines: list[str] = []

    previous_blocked = previous_state.get("last_is_blocked")
    if isinstance(previous_blocked, bool) and previous_blocked != result.is_blocked:
        lines.append(f"Block status changed: {'active' if result.is_blocked else 'inactive'}")

    if result.added_destinations or result.removed_destinations:
        parts = []
        if result.added_destinations:
            parts.append(f"+{result.added_destinations}")
        if result.removed_destinations:
            parts.append(f"-{result.removed_destinations}")
        lines.append(f"IP list updated ({', '.join(parts)})")

    if not lines:
        return None

    header = f"StopLiga route {result.route_name}"
    return header + "\n" + "\n".join(f"- {line}" for line in lines)


def send_notifications(config: Config, result: SyncResult, previous_state: dict[str, object]) -> None:
    if result.dry_run or not config.has_notifications():
        return

    message = build_notification_message(result, previous_state)
    if not message:
        return

    logger = logging.getLogger("stopliga.notify")

    if config.gotify_url and config.gotify_token:
        gotify_url = config.gotify_url.rstrip("/") + "/message"
        _post_json(
            gotify_url,
            {
                "title": "StopLiga",
                "message": message,
                "priority": config.gotify_priority,
                "extras": {"client::display": {"contentType": "text/plain"}},
            },
            timeout=config.request_timeout,
        )
        log_event(logger, logging.INFO, "notification_sent", provider="gotify")

    if config.telegram_bot_token and config.telegram_chat_id:
        telegram_url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
        _post_json(
            telegram_url,
            {
                "chat_id": config.telegram_chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=config.request_timeout,
        )
        log_event(logger, logging.INFO, "notification_sent", provider="telegram")
