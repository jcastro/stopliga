"""General utility helpers."""

from __future__ import annotations

import hashlib
import io
from functools import lru_cache
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
    """Reuse bounded trust-store contexts; refresh on CA changes or hourly."""
    stamp = ca_file.stat().st_mtime_ns if ca_file else 0
    return _ssl_context(verify, str(ca_file) if ca_file else None, stamp, int(time.monotonic() // 3600))


@lru_cache(maxsize=8)
def _ssl_context(verify: bool, ca_file: str | None, stamp: int, epoch: int) -> ssl.SSLContext:
    del stamp, epoch
    if verify:
        return ssl.create_default_context(cafile=ca_file)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
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


class LimitedReader(io.RawIOBase):
    """Bound a streaming binary input, including decompressed data."""

    def __init__(self, stream: Any, max_bytes: int, content_length: str | None = None):
        super().__init__()
        self.stream = stream
        self.remaining = max_bytes
        self.position = 0
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise ValueError("Response exceeds its size limit")
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid or oversized Content-Length") from exc

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        amount = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.read(amount)
        if len(data) > self.remaining:
            raise ValueError("Response exceeds its size limit")
        self.remaining -= len(data)
        self.position += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position
