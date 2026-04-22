"""Notification delivery helpers."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from .errors import NetworkError, NotificationDeliveryError, StopLigaError
from .logging_utils import log_event
from .models import Config, SyncResult
from .utils import make_ssl_context, sleep_with_backoff

DEFAULT_USER_AGENT = "stopliga/0.1.13"


def _safe_notification_url(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path
    if parsed.hostname == "api.telegram.org" and "/bot" in path:
        prefix, _, suffix = path.partition("/bot")
        _, _, after_token = suffix.partition("/")
        path = f"{prefix}/bot***/{after_token}" if after_token else f"{prefix}/bot***"
    return urlunparse((parsed.scheme, netloc, path, "", "", ""))


@dataclass(frozen=True)
class ProviderRequestConfig:
    timeout: float
    retries: int
    verify_tls: bool
    ca_file: Any = None


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    retries: int,
    verify_tls: bool,
    ca_file,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=make_ssl_context(verify=verify_tls, ca_file=ca_file))
    )
    safe_url = _safe_notification_url(url)
    logger = logging.getLogger("stopliga.notify")
    for attempt in range(1, max(1, retries) + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        try:
            with opener.open(request, timeout=timeout):
                return
        except urllib.error.HTTPError as exc:
            if exc.code in {408, 429, 500, 502, 503, 504} and attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "notification_retry",
                    url=safe_url,
                    attempt=attempt,
                    retries=retries,
                    status=exc.code,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Notification request failed for {safe_url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            if attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "notification_retry",
                    url=safe_url,
                    attempt=attempt,
                    retries=retries,
                    error=exc,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Notification request failed for {safe_url}: {exc}") from exc


def _gotify_request_config(config: Config) -> ProviderRequestConfig:
    return ProviderRequestConfig(
        timeout=config.notification_timeout,
        retries=config.notification_retries,
        verify_tls=config.gotify_verify_tls if config.gotify_verify_tls is not None else config.notification_verify_tls,
        ca_file=config.gotify_ca_file or config.notification_ca_file,
    )


def _telegram_request_config(config: Config) -> ProviderRequestConfig:
    return ProviderRequestConfig(
        timeout=config.notification_timeout,
        retries=config.notification_retries,
        verify_tls=config.telegram_verify_tls
        if config.telegram_verify_tls is not None
        else config.notification_verify_tls,
        ca_file=config.telegram_ca_file or config.notification_ca_file,
    )


def _blocked_label(is_blocked: bool) -> str:
    return "ACTIVE" if is_blocked else "INACTIVE"


def build_notification_message(result: SyncResult, previous_state: dict[str, object]) -> str | None:
    changes: list[str] = []

    previous_blocked = previous_state.get("last_is_blocked")
    if isinstance(previous_blocked, bool) and previous_blocked != result.is_blocked:
        changes.append(f"- 🚦 Block status: {_blocked_label(previous_blocked)} -> {_blocked_label(result.is_blocked)}")

    if result.added_destinations or result.removed_destinations:
        parts: list[str] = []
        if result.added_destinations:
            parts.append(f"+{result.added_destinations} added")
        if result.removed_destinations:
            parts.append(f"-{result.removed_destinations} removed")
        changes.append(f"- 🌐 IP list: {', '.join(parts)}")

    if not changes:
        return None

    return "\n".join(
        [
            "🛡️ StopLiga",
            f"📍 Route: {result.route_name}",
            "",
            "Changes detected:",
            *changes,
            "",
            "Current state:",
            f"- {'🔴' if result.is_blocked else '🟢'} Blocking: {_blocked_label(result.is_blocked)}",
            f"- 📦 Desired destinations: {result.desired_destinations}",
        ]
    )


def send_notifications(config: Config, result: SyncResult, previous_state: dict[str, object]) -> None:
    if result.dry_run or not config.has_notifications():
        return

    message = build_notification_message(result, previous_state)
    if not message:
        return

    logger = logging.getLogger("stopliga.notify")
    failures: dict[str, str] = {}

    if config.gotify_url and config.gotify_token:
        try:
            gotify_url = config.gotify_url.rstrip("/") + "/message"
            request_config = _gotify_request_config(config)
            _post_json(
                gotify_url,
                {
                    "title": "StopLiga",
                    "message": message,
                    "priority": config.gotify_priority,
                    "extras": {"client::display": {"contentType": "text/plain"}},
                },
                timeout=request_config.timeout,
                retries=request_config.retries,
                verify_tls=request_config.verify_tls,
                ca_file=request_config.ca_file,
            )
            log_event(logger, logging.INFO, "notification_sent", provider="gotify")
        except StopLigaError as exc:
            failures["gotify"] = str(exc)
            log_event(logger, logging.ERROR, "notification_provider_failed", provider="gotify", error=exc)

    telegram_target = config.resolved_telegram_chat_id()
    if config.telegram_bot_token and telegram_target:
        try:
            telegram_url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
            request_config = _telegram_request_config(config)
            telegram_payload = {
                "chat_id": telegram_target,
                "text": message,
                "disable_web_page_preview": True,
            }
            if config.telegram_topic_id is not None:
                telegram_payload["message_thread_id"] = config.telegram_topic_id
            _post_json(
                telegram_url,
                telegram_payload,
                timeout=request_config.timeout,
                retries=request_config.retries,
                verify_tls=request_config.verify_tls,
                ca_file=request_config.ca_file,
            )
            log_event(logger, logging.INFO, "notification_sent", provider="telegram")
        except StopLigaError as exc:
            failures["telegram"] = str(exc)
            log_event(logger, logging.ERROR, "notification_provider_failed", provider="telegram", error=exc)

    if failures:
        raise NotificationDeliveryError(failures)
