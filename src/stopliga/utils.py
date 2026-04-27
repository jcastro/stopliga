"""General utility helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import random
import ssl
import time
from pathlib import Path
from typing import Any, Iterable


IpTokenSortKey = tuple[int, int, int, int]


def sleep_with_backoff(attempt: int) -> None:
    """Sleep with exponential backoff and bounded jitter."""

    base = min(12.0, 0.5 * (2 ** max(0, attempt - 1)))
    jitter = random.uniform(0.0, 0.5)
    time.sleep(base + jitter)


def stable_hash(value: Any) -> str:
    """Return a deterministic SHA-256 hash for the supplied JSON-serializable value."""

    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compact_json_bytes(value: Any) -> bytes:
    """Serialize a JSON request payload without whitespace."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def canonicalize_ip_token(value: str) -> str:
    """Normalize a single IPv4/IPv6 address or CIDR to a canonical string."""

    token = value.strip()
    if not token:
        raise ValueError("empty token")
    if "/" in token:
        return str(ipaddress.ip_network(token, strict=False))
    return str(ipaddress.ip_address(token))


def ip_token_sort_key(token: str) -> IpTokenSortKey:
    """Return the deterministic ordering key for a canonical IP/CIDR token."""

    if "/" in token:
        network = ipaddress.ip_network(token, strict=False)
        return (network.version, int(network.network_address), network.prefixlen, 1)
    address = ipaddress.ip_address(token)
    return (address.version, int(address), address.max_prefixlen, 0)


def canonicalize_ip_token_with_key(value: str) -> tuple[str, IpTokenSortKey]:
    token = value.strip()
    if not token:
        raise ValueError("empty token")
    if "/" in token:
        network = ipaddress.ip_network(token, strict=False)
        return str(network), (network.version, int(network.network_address), network.prefixlen, 1)
    address = ipaddress.ip_address(token)
    return str(address), (address.version, int(address), address.max_prefixlen, 0)


def sort_ip_tokens(values: Iterable[str]) -> list[str]:
    """Deduplicate and sort IP/CIDR tokens in a deterministic order."""

    keyed_tokens: dict[str, IpTokenSortKey] = {}
    for value in values:
        if not value or not value.strip():
            continue
        token, key = canonicalize_ip_token_with_key(value)
        keyed_tokens.setdefault(token, key)
    return sorted(keyed_tokens, key=keyed_tokens.__getitem__)


def sort_canonical_ip_tokens(values: Iterable[str]) -> list[str]:
    """Deduplicate and sort tokens that are already canonical IP/CIDR strings."""

    keyed_tokens: dict[str, IpTokenSortKey] = {}
    for token in values:
        if not token or not token.strip():
            continue
        key = ip_token_sort_key(token)
        keyed_tokens.setdefault(token, key)
    return sorted(keyed_tokens, key=keyed_tokens.__getitem__)


def make_ssl_context(*, verify: bool, ca_file: Path | None = None) -> ssl.SSLContext:
    """Build an SSL context honoring explicit verification settings."""

    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def read_limited(stream: Any, *, max_bytes: int, content_length: str | None = None) -> bytes:
    """Read a response body with a hard safety ceiling."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        else:
            if declared is not None and declared > max_bytes:
                raise ValueError(f"response content-length {declared} exceeds safety limit {max_bytes}")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(65536, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"response body exceeds safety limit {max_bytes}")
        chunks.append(chunk)
    return b"".join(chunks)


def ensure_parent_dir(path: Path) -> None:
    """Create parent directories for a file path when needed."""

    path.parent.mkdir(parents=True, exist_ok=True)


def shorten_json(data: Any, limit: int = 4000) -> str:
    """Return a shortened pretty JSON representation suitable for logs."""

    text = json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"
