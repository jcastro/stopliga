"""GitHub feed download, validation and normalization."""

from __future__ import annotations

import ipaddress
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

from .errors import InvalidFeedError, NetworkError
from .logging_utils import log_event
from .models import Config, FeedSnapshot
from .utils import canonicalize_ip_token, make_ssl_context, sleep_with_backoff, stable_hash


def _truthy_state(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on", "blocked", "active", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "off", "unblocked", "inactive", "disabled"}:
            return False
    raise InvalidFeedError(f"Unsupported status value: {value!r}")


def parse_status_payload(raw_text: str) -> tuple[dict[str, Any], bool]:
    """Parse the status JSON defensively."""

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidFeedError("Status feed is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidFeedError("Status feed root must be a JSON object")

    if "isBlocked" in payload:
        return payload, _truthy_state(payload["isBlocked"])
    if "blocked" in payload:
        return payload, _truthy_state(payload["blocked"])
    if "state" in payload:
        return payload, _truthy_state(payload["state"])
    raise InvalidFeedError("Status feed does not expose isBlocked, blocked or state")


def parse_ip_list(raw_text: str, *, policy: str) -> tuple[list[str], int, list[str]]:
    """Parse, validate and normalize a TXT IP list feed."""

    valid: set[str] = set()
    invalid: list[str] = []
    raw_lines = 0

    for line in raw_text.splitlines():
        raw_lines += 1
        candidate = line.split("#", 1)[0].strip()
        if not candidate:
            continue
        try:
            valid.add(canonicalize_ip_token(candidate))
        except ValueError:
            if policy == "ignore":
                invalid.append(candidate)
                continue
            raise InvalidFeedError(f"Invalid IP/CIDR entry: {candidate!r}") from None

    ordered = sorted(
        valid,
        key=lambda token: (
            4 if ":" not in token else 6,
            int(ipaddress.ip_network(token, strict=False).network_address),
            ipaddress.ip_network(token, strict=False).prefixlen,
        ),
    )
    return ordered, raw_lines, invalid


def fetch_text(
    url: str,
    *,
    timeout: float,
    retries: int,
    verify_tls: bool,
    ca_file: Any = None,
) -> str:
    """Fetch a text payload over HTTP(S) with retries and explicit TLS control."""

    logger = logging.getLogger("stopliga.feed")
    context = make_ssl_context(verify=verify_tls, ca_file=ca_file)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    last_error: Exception | None = None

    for attempt in range(1, max(1, retries) + 1):
        request = urllib.request.Request(url, headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.1"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            last_error = exc
            if attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "feed_retry",
                    url=url,
                    attempt=attempt,
                    retries=retries,
                    error=exc,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Unable to fetch {url}: {exc}") from exc
    raise NetworkError(f"Unable to fetch {url}: {last_error}")


def load_feed_snapshot(config: Config) -> FeedSnapshot:
    """Download and validate the status + IP feed."""

    logger = logging.getLogger("stopliga.feed")
    raw_status_text = fetch_text(
        config.status_url,
        timeout=config.request_timeout,
        retries=config.retries,
        verify_tls=config.feed_verify_tls,
        ca_file=config.feed_ca_file,
    )
    raw_status, is_blocked = parse_status_payload(raw_status_text)

    raw_ip_text = fetch_text(
        config.ip_list_url,
        timeout=config.request_timeout,
        retries=config.retries,
        verify_tls=config.feed_verify_tls,
        ca_file=config.feed_ca_file,
    )
    destinations, raw_lines, invalid_entries = parse_ip_list(
        raw_ip_text,
        policy=config.invalid_entry_policy,
    )
    if not destinations:
        raise InvalidFeedError("IP list feed produced zero valid destinations")
    if len(destinations) > config.max_destinations:
        raise InvalidFeedError(
            f"Validated destination count {len(destinations)} exceeds configured safety ceiling {config.max_destinations}"
        )

    desired_enabled = is_blocked if config.enable_when_blocked else not is_blocked
    snapshot = FeedSnapshot(
        is_blocked=is_blocked,
        desired_enabled=desired_enabled,
        destinations=destinations,
        raw_status=raw_status,
        raw_line_count=raw_lines,
        valid_count=len(destinations),
        invalid_count=len(invalid_entries),
        invalid_entries=invalid_entries,
        destinations_hash=stable_hash(destinations),
        feed_hash=stable_hash(
            {
                "status": raw_status,
                "desired_enabled": desired_enabled,
                "destinations": destinations,
            }
        ),
    )
    log_event(
        logger,
        logging.INFO,
        "feed_loaded",
        is_blocked=is_blocked,
        desired_enabled=desired_enabled,
        raw_lines=raw_lines,
        valid_destinations=snapshot.valid_count,
        invalid_destinations=snapshot.invalid_count,
        feed_hash=snapshot.feed_hash,
    )
    if snapshot.invalid_count:
        log_event(
            logger,
            logging.WARNING,
            "feed_invalid_entries_ignored",
            invalid_count=snapshot.invalid_count,
        )
    return snapshot
