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


def sleep_with_backoff(attempt: int) -> None:
    """Sleep with exponential backoff and bounded jitter."""

    base = min(12.0, 0.5 * (2 ** max(0, attempt - 1)))
    jitter = random.uniform(0.0, 0.5)
    time.sleep(base + jitter)


def stable_hash(value: Any) -> str:
    """Return a deterministic SHA-256 hash for the supplied JSON-serializable value."""

    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonicalize_ip_token(value: str) -> str:
    """Normalize a single IPv4/IPv6 address or CIDR to a canonical string."""

    token = value.strip()
    if not token:
        raise ValueError("empty token")
    if "/" in token:
        return str(ipaddress.ip_network(token, strict=False))
    return str(ipaddress.ip_address(token))


def sort_ip_tokens(values: Iterable[str]) -> list[str]:
    """Deduplicate and sort IP/CIDR tokens in a deterministic order."""

    unique = {canonicalize_ip_token(value) for value in values if value and value.strip()}

    def sort_key(token: str) -> tuple[int, int, int, int]:
        network = ipaddress.ip_network(token, strict=False)
        return (
            network.version,
            int(network.network_address),
            network.prefixlen,
            0 if "/" not in token else 1,
        )

    return sorted(unique, key=sort_key)


def make_ssl_context(*, verify: bool, ca_file: Path | None = None) -> ssl.SSLContext:
    """Build an SSL context honoring explicit verification settings."""

    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def ensure_parent_dir(path: Path) -> None:
    """Create parent directories for a file path when needed."""

    path.parent.mkdir(parents=True, exist_ok=True)


def shorten_json(data: Any, limit: int = 4000) -> str:
    """Return a shortened pretty JSON representation suitable for logs."""

    text = json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"
