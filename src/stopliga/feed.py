"""Feed download, validation and normalization."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from .errors import InvalidFeedError, NetworkError
from .logging_utils import log_event
from .models import Config, FeedSnapshot
from .utils import canonicalize_ip_token, make_ssl_context, read_limited, sleep_with_backoff, stable_hash, sort_ip_tokens


@dataclass(frozen=True)
class GitHubRawFile:
    owner: str
    repo: str
    ref: str
    path: str

    def resolved_url(self, resolved_ref: str) -> str:
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{resolved_ref}/{self.path}"


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
    hayahora_status = _parse_hayahora_status_payload(payload)
    if hayahora_status is not None:
        return hayahora_status
    raise InvalidFeedError("Status feed does not expose isBlocked, blocked, state or a supported hayahora history payload")


def _parse_hayahora_status_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
    data = payload.get("data")
    if not isinstance(data, list):
        return None

    active_ips: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ip_value = entry.get("ip")
        if not isinstance(ip_value, str) or not ip_value.strip():
            continue
        state_changes = entry.get("stateChanges")
        if not isinstance(state_changes, list) or not state_changes:
            continue

        latest_state: bool | None = None
        for change in state_changes:
            if not isinstance(change, dict) or "state" not in change:
                continue
            latest_state = _truthy_state(change["state"])
        if latest_state:
            try:
                active_ips.add(canonicalize_ip_token(ip_value))
            except ValueError:
                continue

    active_ip_list = sort_ip_tokens(active_ips)
    is_blocked = bool(active_ip_list)
    summarized_payload: dict[str, Any] = {
        "source": "hayahora-history-json",
        "lastUpdate": payload.get("lastUpdate"),
        "blocked": is_blocked,
        "activeIpCount": len(active_ip_list),
    }
    if active_ip_list:
        summarized_payload["activeIpSample"] = active_ip_list[:5]
    return summarized_payload, is_blocked


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
        request = urllib.request.Request(url, headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.1"})
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


def resolve_dns_addresses(hostname: str, *, retries: int) -> list[str]:
    """Resolve a DNS hostname into a deterministic list of IP addresses."""

    logger = logging.getLogger("stopliga.feed")
    last_error: Exception | None = None

    for attempt in range(1, max(1, retries) + 1):
        try:
            answers = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
            addresses = sort_ip_tokens(
                answer[4][0]
                for answer in answers
                if len(answer) >= 5 and isinstance(answer[4], tuple) and answer[4] and answer[4][0]
            )
            return [address for address in addresses if "/" not in address]
        except (socket.gaierror, OSError, ValueError) as exc:
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
    dns_host = _parse_dns_feed_host(config.status_url)
    if dns_host is not None:
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


def _parse_github_raw_file(url: str) -> GitHubRawFile | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    if parsed.netloc != "raw.githubusercontent.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return None
    owner, repo, ref = parts[:3]
    path = "/".join(parts[3:])
    if not all((owner, repo, ref, path)):
        return None
    return GitHubRawFile(owner=owner, repo=repo, ref=ref, path=path)


def _resolve_github_commit_sha(
    owner: str,
    repo: str,
    ref: str,
    *,
    timeout: float,
    retries: int,
    verify_tls: bool,
    max_bytes: int = 256 * 1024,
    ca_file: Any = None,
) -> str:
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{quote(ref, safe='')}"
    payload = fetch_text(
        api_url,
        timeout=timeout,
        retries=retries,
        verify_tls=verify_tls,
        max_bytes=max_bytes,
        ca_file=ca_file,
    )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InvalidFeedError(f"GitHub commit lookup returned invalid JSON for {owner}/{repo}@{ref}") from exc
    if not isinstance(parsed, dict):
        raise InvalidFeedError(f"GitHub commit lookup returned an invalid payload for {owner}/{repo}@{ref}")
    sha = parsed.get("sha")
    if not isinstance(sha, str) or not sha.strip():
        raise InvalidFeedError(f"GitHub commit lookup did not return a sha for {owner}/{repo}@{ref}")
    return sha.strip()


def _resolve_consistent_feed_urls(config: Config) -> tuple[str, str, str | None]:
    status_ref = _parse_github_raw_file(config.status_url)
    ip_ref = _parse_github_raw_file(config.ip_list_url)
    if status_ref is None or ip_ref is None:
        return config.status_url, config.ip_list_url, None
    if (status_ref.owner, status_ref.repo, status_ref.ref) != (ip_ref.owner, ip_ref.repo, ip_ref.ref):
        return config.status_url, config.ip_list_url, None

    try:
        sha = _resolve_github_commit_sha(
            status_ref.owner,
            status_ref.repo,
            status_ref.ref,
            timeout=config.request_timeout,
            retries=config.retries,
            verify_tls=config.feed_verify_tls,
            max_bytes=config.max_response_bytes,
            ca_file=config.feed_ca_file,
        )
    except (InvalidFeedError, NetworkError) as exc:
        if config.strict_feed_consistency:
            raise
        log_event(
            logging.getLogger("stopliga.feed"),
            logging.WARNING,
            "feed_revision_resolution_degraded",
            owner=status_ref.owner,
            repo=status_ref.repo,
            ref=status_ref.ref,
            error=exc,
        )
        return config.status_url, config.ip_list_url, None
    return status_ref.resolved_url(sha), ip_ref.resolved_url(sha), sha


def load_feed_snapshot(config: Config) -> FeedSnapshot:
    """Download and validate the status + IP feed."""

    logger = logging.getLogger("stopliga.feed")
    status_url, ip_list_url, source_revision = _resolve_consistent_feed_urls(config)
    log_event(
        logger,
        logging.INFO,
        "feed_check",
        status_url=_safe_log_url(status_url),
        ip_list_url=_safe_log_url(ip_list_url),
        strict_consistency=config.strict_feed_consistency,
    )
    if source_revision:
        log_event(logger, logging.INFO, "feed_revision_resolved", revision=source_revision)
    elif config.status_url != status_url or config.ip_list_url != ip_list_url:
        log_event(logger, logging.WARNING, "feed_revision_resolution_skipped", status_url=config.status_url, ip_list_url=config.ip_list_url)
    status_config = config if status_url == config.status_url else replace(config, status_url=status_url)
    raw_status, is_blocked = load_status_snapshot(status_config)

    raw_ip_text = fetch_text(
        ip_list_url,
        timeout=config.request_timeout,
        retries=config.retries,
        verify_tls=config.feed_verify_tls,
        max_bytes=config.max_response_bytes,
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

    desired_enabled = is_blocked
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
        revision=source_revision,
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
