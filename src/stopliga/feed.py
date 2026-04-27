"""Feed download, validation and normalization."""

from __future__ import annotations

import heapq
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from .errors import InvalidFeedError, NetworkError
from .logging_utils import log_event
from .models import Config, FeedSnapshot
from .utils import (
    canonicalize_ip_token,
    canonicalize_ip_token_with_key,
    ip_token_sort_key,
    IpTokenSortKey,
    make_ssl_context,
    read_limited,
    sleep_with_backoff,
    stable_hash,
    sort_ip_tokens,
)

DEFAULT_USER_AGENT = "stopliga/0.1.25"
HAYAHORA_DNS_STATUS_HOST = "blocked.dns.hayahora.futbol"
HAYAHORA_STATUS_JSON_URL = "https://hayahora.futbol/estado/data.json"
# Hayahora's canonical JSON feed is historical and keeps growing over time,
# so keep a larger ceiling here than the generic response limit.
HAYAHORA_STATUS_MAX_BYTES = 16 * 1024 * 1024
# Match Hayahora's public site hero logic ("NO"/"SI") as observed in the
# current frontend bundle on 2026-04-23. This is stricter than treating any
# single active ISP entry as a global block signal.
HAYAHORA_HERO_DESCRIPTION = "Cloudflare"
HAYAHORA_HERO_MIN_PROVIDER_MATCHES = 3
HAYAHORA_HERO_MIN_CONFIRMED_IPS = 11
HAYAHORA_HERO_SENTINEL_IPS = frozenset({"188.114.96.5", "188.114.97.5"})


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


def _sample_canonical_ip_tokens(values: Any, *, limit: int = 5) -> list[str]:
    if limit < 1:
        return []
    unique = {value for value in values if isinstance(value, str) and value.strip()}
    return heapq.nsmallest(limit, unique, key=ip_token_sort_key)


def parse_status_payload_value(payload: Any) -> tuple[dict[str, Any], bool]:
    """Parse a decoded status payload defensively."""

    if not isinstance(payload, dict):
        raise InvalidFeedError("Status feed root must be a JSON object")

    if "isBlocked" in payload:
        return payload, _truthy_state(payload["isBlocked"])
    if "blocked" in payload:
        return payload, _truthy_state(payload["blocked"])
    if "state" in payload:
        return payload, _truthy_state(payload["state"])
    hayahora_status = _parse_hayahora_status_payload(payload)
    if hayahora_status is not None:
        return hayahora_status
    raise InvalidFeedError(
        "Status feed does not expose isBlocked, blocked, state or a supported hayahora history payload"
    )


def parse_status_payload(raw_text: str) -> tuple[dict[str, Any], bool]:
    """Parse the status JSON defensively."""

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidFeedError("Status feed is not valid JSON") from exc
    return parse_status_payload_value(payload)


def _parse_hayahora_status_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
    data = payload.get("data")
    if not isinstance(data, list):
        return None

    active_cloudflare_counts: dict[str, int] = {}
    sentinel_hits: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ip_value = entry.get("ip")
        if not isinstance(ip_value, str) or not ip_value.strip():
            continue
        description = entry.get("description")
        if description != HAYAHORA_HERO_DESCRIPTION:
            continue
        state_changes = entry.get("stateChanges")
        if not isinstance(state_changes, list) or not state_changes:
            continue

        latest_state: bool | None = None
        for change in reversed(state_changes):
            if not isinstance(change, dict) or "state" not in change:
                continue
            latest_state = _truthy_state(change["state"])
            break
        if latest_state is None:
            continue
        try:
            normalized_ip = canonicalize_ip_token(ip_value)
        except ValueError:
            continue
        if not latest_state:
            continue

        active_cloudflare_counts[normalized_ip] = active_cloudflare_counts.get(normalized_ip, 0) + 1
        if normalized_ip in HAYAHORA_HERO_SENTINEL_IPS:
            sentinel_hits.add(normalized_ip)

    active_ip_count = len(active_cloudflare_counts)
    confirmed_ips = {
        ip
        for ip, provider_matches in active_cloudflare_counts.items()
        if provider_matches >= HAYAHORA_HERO_MIN_PROVIDER_MATCHES
    }
    confirmed_ip_count = len(confirmed_ips)
    sentinel_pair_blocked = HAYAHORA_HERO_SENTINEL_IPS.issubset(sentinel_hits)
    is_blocked = confirmed_ip_count >= HAYAHORA_HERO_MIN_CONFIRMED_IPS or sentinel_pair_blocked
    summarized_payload: dict[str, Any] = {
        "source": "hayahora-history-json",
        "lastUpdate": payload.get("lastUpdate"),
        "blocked": is_blocked,
        "activeIpCount": active_ip_count,
        "confirmedIpCount": confirmed_ip_count,
        "strategy": "hayahora-site-hero",
        "providerMatchThreshold": HAYAHORA_HERO_MIN_PROVIDER_MATCHES,
        "minConfirmedIpCount": HAYAHORA_HERO_MIN_CONFIRMED_IPS,
        "sentinelPairBlocked": sentinel_pair_blocked,
        "sentinelPair": sorted(HAYAHORA_HERO_SENTINEL_IPS),
    }
    if active_ip_count:
        summarized_payload["activeIpSample"] = _sample_canonical_ip_tokens(active_cloudflare_counts.keys())
    if confirmed_ip_count:
        summarized_payload["confirmedIpSample"] = _sample_canonical_ip_tokens(confirmed_ips)
    if sentinel_hits:
        summarized_payload["sentinelPairHitSample"] = sorted(sentinel_hits, key=ip_token_sort_key)
    return summarized_payload, is_blocked


def normalize_hayahora_isp(value: Any) -> str | None:
    """Normalize Hayahora ISP labels for forgiving user configuration."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().casefold().split())
    return normalized or None


def _parse_hayahora_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if "T" not in raw and " " not in raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hayahora_reference_time(payload: dict[str, Any]) -> datetime:
    parsed = _parse_hayahora_timestamp(payload.get("lastUpdate"))
    if parsed is not None:
        return parsed

    latest: datetime | None = None
    data = payload.get("data")
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("stateChanges")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                timestamp = _parse_hayahora_timestamp(change.get("timestamp"))
                if timestamp is not None and (latest is None or timestamp > latest):
                    latest = timestamp
    return latest or datetime.now(timezone.utc)


def extract_hayahora_active_ips(
    payload: dict[str, Any],
    *,
    isp: str | None = None,
    lookback_hours: int = 24,
    invalid_entry_policy: str = "fail",
    max_destinations: int | None = None,
) -> tuple[list[str], int, list[str]]:
    """Extract currently active Hayahora destinations, optionally filtered by ISP."""

    data = payload.get("data")
    if not isinstance(data, list):
        raise InvalidFeedError("Hayahora active destination mode requires a status payload with a data list")
    if lookback_hours < 1:
        raise InvalidFeedError("Hayahora lookback window must be >= 1 hour")

    target_isp = normalize_hayahora_isp(isp)
    cutoff = _hayahora_reference_time(payload) - timedelta(hours=lookback_hours)
    available_isps: dict[str, str] = {}
    valid: dict[str, IpTokenSortKey] = {}
    invalid: list[str] = []
    inspected = 0

    for entry in data:
        if not isinstance(entry, dict):
            continue
        inspected += 1
        if target_isp is not None:
            normalized_entry_isp = normalize_hayahora_isp(entry.get("isp"))
            if normalized_entry_isp is not None:
                available_isps.setdefault(normalized_entry_isp, str(entry.get("isp")).strip())
            if normalized_entry_isp != target_isp:
                continue
        state_changes = entry.get("stateChanges")
        if not isinstance(state_changes, list) or not state_changes:
            continue
        latest = state_changes[-1]
        if not isinstance(latest, dict) or "state" not in latest:
            continue
        latest_timestamp = _parse_hayahora_timestamp(latest.get("timestamp"))
        if latest_timestamp is None or latest_timestamp < cutoff:
            continue
        if not _truthy_state(latest["state"]):
            continue
        ip_value = entry.get("ip")
        if not isinstance(ip_value, str) or not ip_value.strip():
            continue
        try:
            token, key = canonicalize_ip_token_with_key(ip_value)
            valid.setdefault(token, key)
        except ValueError:
            if invalid_entry_policy == "ignore":
                invalid.append(ip_value)
                continue
            raise InvalidFeedError(f"Invalid IP/CIDR entry: {ip_value!r}") from None
        if max_destinations is not None and len(valid) > max_destinations:
            raise InvalidFeedError(f"Validated destination count exceeds configured safety ceiling {max_destinations}")

    if target_isp is not None and target_isp not in available_isps:
        valid_options = ", ".join(sorted(available_isps.values(), key=str.casefold))
        raise InvalidFeedError(f"Unknown Hayahora ISP {isp!r}. Valid options: {valid_options}")

    return sorted(valid, key=valid.__getitem__), inspected, invalid


def parse_ip_list(
    raw_text: str, *, policy: str, max_destinations: int | None = None
) -> tuple[list[str], int, list[str]]:
    """Parse, validate and normalize a TXT IP list feed."""

    valid: dict[str, IpTokenSortKey] = {}
    invalid: list[str] = []
    raw_lines = 0

    for line in raw_text.splitlines():
        raw_lines += 1
        candidate = line.partition("#")[0].strip()
        if not candidate:
            continue
        try:
            token, key = canonicalize_ip_token_with_key(candidate)
            valid.setdefault(token, key)
        except ValueError:
            if policy == "ignore":
                invalid.append(candidate)
                continue
            raise InvalidFeedError(f"Invalid IP/CIDR entry: {candidate!r}") from None
        if max_destinations is not None and len(valid) > max_destinations:
            raise InvalidFeedError(f"Validated destination count exceeds configured safety ceiling {max_destinations}")

    ordered = sorted(valid, key=valid.__getitem__)
    return ordered, raw_lines, invalid


def fetch_text(
    url: str,
    *,
    timeout: float,
    retries: int,
    verify_tls: bool,
    max_bytes: int,
    ca_file: Any = None,
) -> str:
    """Fetch a text payload over HTTP(S) with retries and explicit TLS control."""

    logger = logging.getLogger("stopliga.feed")
    context = make_ssl_context(verify=verify_tls, ca_file=ca_file)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    last_error: Exception | None = None
    safe_url = _safe_log_url(url)

    for attempt in range(1, max(1, retries) + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.1",
                "User-Agent": DEFAULT_USER_AGENT,
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                try:
                    body = read_limited(
                        response,
                        max_bytes=max_bytes,
                        content_length=response.headers.get("Content-Length"),
                    )
                except ValueError as exc:
                    raise NetworkError(f"Unable to fetch {safe_url}: {exc}") from exc
                return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            retryable_http = exc.code in {408, 429, 500, 502, 503, 504}
            if retryable_http and attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "feed_retry",
                    url=safe_url,
                    attempt=attempt,
                    retries=retries,
                    status=exc.code,
                    error=exc,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Unable to fetch {safe_url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            last_error = exc
            if attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "feed_retry",
                    url=safe_url,
                    attempt=attempt,
                    retries=retries,
                    error=exc,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Unable to fetch {safe_url}: {exc}") from exc
    raise NetworkError(f"Unable to fetch {safe_url}: {last_error}")


def _parse_dns_feed_host(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "dns":
        return None
    hostname = parsed.hostname
    if not hostname:
        raise InvalidFeedError("DNS feed URL must include a hostname")
    return hostname


def _is_dns_no_records_error(exc: socket.gaierror) -> bool:
    no_record_errnos = {
        getattr(socket, "EAI_NONAME", None),
        getattr(socket, "EAI_NODATA", None),
    }
    return exc.errno in {value for value in no_record_errnos if value is not None}


def _load_hayahora_canonical_status(config: Config) -> tuple[dict[str, Any], bool]:
    raw_status_text = fetch_text(
        HAYAHORA_STATUS_JSON_URL,
        timeout=config.request_timeout,
        retries=config.retries,
        verify_tls=config.feed_verify_tls,
        max_bytes=max(config.max_response_bytes, HAYAHORA_STATUS_MAX_BYTES),
        ca_file=config.feed_ca_file,
    )
    return parse_status_payload(raw_status_text)


def _load_structured_hayahora_status(config: Config) -> dict[str, Any]:
    status_url = config.status_url
    dns_host = _parse_dns_feed_host(status_url)
    if dns_host == HAYAHORA_DNS_STATUS_HOST:
        status_url = HAYAHORA_STATUS_JSON_URL

    raw_status_text = fetch_text(
        status_url,
        timeout=config.request_timeout,
        retries=config.retries,
        verify_tls=config.feed_verify_tls,
        max_bytes=max(config.max_response_bytes, HAYAHORA_STATUS_MAX_BYTES)
        if status_url == HAYAHORA_STATUS_JSON_URL
        else config.max_response_bytes,
        ca_file=config.feed_ca_file,
    )
    try:
        payload = json.loads(raw_status_text)
    except json.JSONDecodeError as exc:
        raise InvalidFeedError("Hayahora active destination mode requires a JSON status payload") from exc
    if not isinstance(payload, dict):
        raise InvalidFeedError("Hayahora active destination mode requires a JSON object status payload")
    if not isinstance(payload.get("data"), list):
        raise InvalidFeedError("Hayahora active destination mode requires a status payload with a data list")
    return payload


def _summarize_hayahora_active_status(
    payload: dict[str, Any], *, is_blocked: bool, active_ip_count: int, inspected_entries: int
) -> dict[str, Any]:
    return {
        "source": "hayahora-history-json",
        "lastUpdate": payload.get("lastUpdate"),
        "blocked": is_blocked,
        "activeIpCount": active_ip_count,
        "inspectedEntryCount": inspected_entries,
        "strategy": "hayahora-active-destinations",
    }


def resolve_dns_addresses(hostname: str, *, retries: int) -> list[str]:
    """Resolve a DNS hostname into a deterministic list of IP addresses."""

    logger = logging.getLogger("stopliga.feed")
    last_error: Exception | None = None

    for attempt in range(1, max(1, retries) + 1):
        try:
            answers = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
            resolved_values: list[str] = []
            for answer in answers:
                if len(answer) < 5 or not isinstance(answer[4], tuple) or not answer[4]:
                    continue
                candidate = answer[4][0]
                if isinstance(candidate, str) and candidate:
                    resolved_values.append(candidate)
            addresses = sort_ip_tokens(resolved_values)
            return [address for address in addresses if "/" not in address]
        except socket.gaierror as exc:
            if _is_dns_no_records_error(exc):
                log_event(logging.getLogger("stopliga.feed"), logging.INFO, "dns_no_records", url=f"dns://{hostname}")
                return []
            last_error = exc
            if attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "feed_retry",
                    url=f"dns://{hostname}",
                    attempt=attempt,
                    retries=retries,
                    error=exc,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Unable to resolve dns://{hostname}: {exc}") from exc
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                log_event(
                    logger,
                    logging.WARNING,
                    "feed_retry",
                    url=f"dns://{hostname}",
                    attempt=attempt,
                    retries=retries,
                    error=exc,
                )
                sleep_with_backoff(attempt)
                continue
            raise NetworkError(f"Unable to resolve dns://{hostname}: {exc}") from exc
    raise NetworkError(f"Unable to resolve dns://{hostname}: {last_error}")


def load_status_snapshot(config: Config) -> tuple[dict[str, Any], bool]:
    if config.status_url == HAYAHORA_STATUS_JSON_URL:
        return _load_hayahora_canonical_status(config)

    dns_host = _parse_dns_feed_host(config.status_url)
    if dns_host is not None:
        if dns_host == HAYAHORA_DNS_STATUS_HOST:
            try:
                return _load_hayahora_canonical_status(config)
            except (InvalidFeedError, NetworkError) as exc:
                log_event(
                    logging.getLogger("stopliga.feed"),
                    logging.WARNING,
                    "feed_canonical_status_fallback",
                    dns_host=dns_host,
                    canonical_url=HAYAHORA_STATUS_JSON_URL,
                    error=exc,
                )
        resolved_ips = resolve_dns_addresses(dns_host, retries=config.retries)
        is_blocked = bool(resolved_ips)
        return {
            "source": "dns",
            "hostname": dns_host,
            "blocked": is_blocked,
            "recordCount": len(resolved_ips),
            "recordSample": resolved_ips[:5],
        }, is_blocked

    raw_status_text = fetch_text(
        config.status_url,
        timeout=config.request_timeout,
        retries=config.retries,
        verify_tls=config.feed_verify_tls,
        max_bytes=config.max_response_bytes,
        ca_file=config.feed_ca_file,
    )
    return parse_status_payload(raw_status_text)


def _safe_log_url(url: str) -> str:
    parsed = urlparse(url)
    redacted_netloc = parsed.hostname or ""
    if parsed.port:
        redacted_netloc = f"{redacted_netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, redacted_netloc, parsed.path, "", "", ""))


def load_feed_snapshot(config: Config) -> FeedSnapshot:
    """Download and validate the Hayahora status feed."""

    logger = logging.getLogger("stopliga.feed")
    log_event(
        logger,
        logging.INFO,
        "feed_check",
        status_url=_safe_log_url(config.status_url),
    )
    status_payload = _load_structured_hayahora_status(config)
    destinations, raw_lines, invalid_entries = extract_hayahora_active_ips(
        status_payload,
        isp=config.hayahora_isp,
        lookback_hours=config.hayahora_lookback_hours,
        invalid_entry_policy=config.invalid_entry_policy,
        max_destinations=config.max_destinations,
    )
    desired_enabled = bool(destinations)
    is_blocked = desired_enabled
    raw_status = _summarize_hayahora_active_status(
        status_payload,
        is_blocked=is_blocked,
        active_ip_count=len(destinations),
        inspected_entries=raw_lines,
    )
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
        destination_source="hayahora_active",
        hayahora_isp=config.hayahora_isp,
        hayahora_lookback_hours=config.hayahora_lookback_hours,
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
