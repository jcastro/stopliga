"""Keenetic RCI static-route backend."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import logging
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from ..errors import AuthenticationError, NetworkError, RemoteRequestError
from ..logging_utils import log_event
from ..models import Config, FeedSnapshot, SyncResult
from ..utils import make_ssl_context, read_limited, sleep_with_backoff
from .base import BootstrapGuardClearer, BootstrapGuardWriter, RouterDriver


BACKEND_NAME = "keenetic-static-routes"


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _collapse_destinations(destinations: list[str]) -> list[ipaddress.IPv4Network]:
    networks = []
    for token in destinations:
        normalized = token.strip()
        if not normalized:
            continue
        if "/" in normalized:
            network = ipaddress.ip_network(normalized, strict=False)
        else:
            network = ipaddress.ip_network(f"{normalized}/32", strict=False)
        if network.version != 4:
            raise RemoteRequestError("Keenetic backend currently supports only IPv4 destinations")
        networks.append(network)
    collapsed = ipaddress.collapse_addresses(networks)
    return sorted(
        (network for network in collapsed if isinstance(network, ipaddress.IPv4Network)),
        key=lambda network: (int(network.network_address), network.prefixlen),
    )


def _route_token(network: ipaddress.IPv4Network) -> str:
    return str(network)


@dataclass(frozen=True)
class _RouteEntry:
    destination: ipaddress.IPv4Network
    interface: str | None
    gateway: str | None
    auto: bool
    reject: bool


class KeeneticClient:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.keenetic")
        self.base_url = (config.keenetic_base_url or "").rstrip("/")
        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            self.base_url,
            config.keenetic_username or "",
            config.keenetic_password or "",
        )
        context = make_ssl_context(verify=config.keenetic_verify_tls, ca_file=config.keenetic_ca_file)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(password_manager),
            urllib.request.HTTPSHandler(context=context),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        encoded_query = ""
        if query:
            encoded_query = "?" + urllib.parse.urlencode(query)
        url = f"{self.base_url}{path}{encoded_query}"
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        last_error: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            try:
                with self.opener.open(request, timeout=self.config.request_timeout) as response:
                    raw = read_limited(
                        response,
                        max_bytes=self.config.max_response_bytes,
                        content_length=response.headers.get("Content-Length"),
                    )
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code in {401, 403}:
                    raise AuthenticationError("Keenetic authentication failed") from exc
                if exc.code >= 500 and attempt < self.config.retries:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "keenetic_retry_http",
                        attempt=attempt,
                        status=exc.code,
                        path=path,
                    )
                    sleep_with_backoff(attempt)
                    continue
                details = raw.decode("utf-8", errors="replace") if raw else f"HTTP {exc.code}"
                raise RemoteRequestError(f"Keenetic request {method} {path} failed: {details}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.retries:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "keenetic_retry_network",
                        attempt=attempt,
                        path=path,
                        error=exc,
                    )
                    sleep_with_backoff(attempt)
                    continue
                raise NetworkError(f"Keenetic request {method} {path} failed: {exc}") from exc
        raise NetworkError(f"Keenetic request {method} {path} failed: {last_error}")

    def list_routes(self) -> list[_RouteEntry]:
        payload = self.request("GET", "/rci/ip/route")
        if payload is None:
            return []
        records: list[dict[str, Any]]
        if isinstance(payload, list):
            records = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            raw_routes = payload.get("route")
            if isinstance(raw_routes, list):
                records = [item for item in raw_routes if isinstance(item, dict)]
            else:
                records = [payload]
        else:
            raise RemoteRequestError("Keenetic /rci/ip/route returned an unexpected payload")

        routes: list[_RouteEntry] = []
        for record in records:
            destination = self._destination_from_record(record)
            if destination is None:
                continue
            routes.append(
                _RouteEntry(
                    destination=destination,
                    interface=_normalize_text(record.get("interface")),
                    gateway=_normalize_text(record.get("gateway")),
                    auto=_truthy(record.get("auto")),
                    reject=_truthy(record.get("reject")),
                )
            )
        return routes

    @staticmethod
    def _destination_from_record(record: dict[str, Any]) -> ipaddress.IPv4Network | None:
        host = _normalize_text(record.get("host"))
        if host is not None:
            host_network = ipaddress.ip_network(f"{host}/32", strict=False)
            return host_network if isinstance(host_network, ipaddress.IPv4Network) else None
        network = _normalize_text(record.get("network"))
        mask = _normalize_text(record.get("mask"))
        if network is None or mask is None:
            return None
        parsed = ipaddress.ip_network(f"{network}/{mask}", strict=False)
        return parsed if isinstance(parsed, ipaddress.IPv4Network) else None

    def create_route(self, destination: ipaddress.IPv4Network) -> None:
        self.request("POST", "/rci/ip/route", payload=self._route_payload(destination))

    def delete_route(self, destination: ipaddress.IPv4Network) -> None:
        self.request("DELETE", "/rci/ip/route", query=self._route_delete_query(destination))

    def _route_payload(self, destination: ipaddress.IPv4Network) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "interface": self.config.keenetic_interface or "",
        }
        if destination.prefixlen == 32:
            payload["host"] = str(destination.network_address)
        else:
            payload["network"] = str(destination.network_address)
            payload["mask"] = str(destination.netmask)
        if self.config.keenetic_gateway:
            payload["gateway"] = self.config.keenetic_gateway
        if self.config.keenetic_auto:
            payload["auto"] = True
        if self.config.keenetic_reject:
            payload["reject"] = True
        return payload

    def _route_delete_query(self, destination: ipaddress.IPv4Network) -> dict[str, str]:
        query: dict[str, str] = {"interface": self.config.keenetic_interface or ""}
        if destination.prefixlen == 32:
            query["host"] = str(destination.network_address)
        else:
            query["network"] = str(destination.network_address)
            query["mask"] = str(destination.netmask)
        if self.config.keenetic_gateway:
            query["gateway"] = self.config.keenetic_gateway
        return query


@dataclass(frozen=True)
class KeeneticRouterDriver(RouterDriver):
    config: Config
    router_type: str = "keenetic"

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger("stopliga.keenetic")

    def sync(
        self,
        feed_snapshot: FeedSnapshot,
        previous_guard: dict[str, object],
        *,
        guard_writer: BootstrapGuardWriter,
        guard_clearer: BootstrapGuardClearer,
    ) -> SyncResult:
        del previous_guard
        del guard_writer
        del guard_clearer

        client = KeeneticClient(self.config)
        collapsed_feed = _collapse_destinations(feed_snapshot.destinations)
        desired_destinations = collapsed_feed if feed_snapshot.desired_enabled else []
        desired_tokens = {_route_token(route) for route in desired_destinations}

        current_routes = client.list_routes()
        managed_routes = self._managed_routes(current_routes)
        current_tokens = set(managed_routes)

        added_destinations = len(desired_tokens - current_tokens)
        removed_destinations = len(current_tokens - desired_tokens)
        changed = added_destinations > 0 or removed_destinations > 0
        current_enabled = bool(managed_routes) if managed_routes else None

        summary = (
            f"interface={self.config.keenetic_interface} gateway={self.config.keenetic_gateway or '-'} "
            f"routes={len(desired_destinations)} feed_routes={len(collapsed_feed)} "
            f"added={added_destinations} removed={removed_destinations}"
        )

        log_event(
            self.logger,
            logging.INFO,
            "keenetic_route_check",
            backend=BACKEND_NAME,
            current_destinations=len(current_tokens),
            desired_destinations=len(desired_tokens),
            current_enabled=current_enabled,
            desired_enabled=feed_snapshot.desired_enabled,
            interface=self.config.keenetic_interface,
        )

        if self.config.dry_run:
            return SyncResult(
                mode="keenetic",
                route_name=self.config.route_name,
                route_id=self.config.keenetic_interface,
                backend_name=BACKEND_NAME,
                changed=changed,
                created=added_destinations > 0,
                dry_run=True,
                desired_enabled=feed_snapshot.desired_enabled,
                current_enabled=current_enabled,
                desired_destinations=len(desired_destinations),
                current_destinations=len(current_tokens),
                invalid_entries=feed_snapshot.invalid_count,
                feed_hash=feed_snapshot.feed_hash,
                destinations_hash=feed_snapshot.destinations_hash,
                summary=summary,
                is_blocked=feed_snapshot.is_blocked,
                added_destinations=added_destinations,
                removed_destinations=removed_destinations,
            )

        created = False
        for destination in desired_destinations:
            token = _route_token(destination)
            if token in managed_routes:
                continue
            client.create_route(destination)
            created = True

        for token, route in managed_routes.items():
            if token in desired_tokens:
                continue
            client.delete_route(route.destination)

        refreshed_routes = self._managed_routes(client.list_routes())
        if set(refreshed_routes) != desired_tokens:
            raise RemoteRequestError("Keenetic routes were not reconciled to the expected destination set")

        return SyncResult(
            mode="keenetic",
            route_name=self.config.route_name,
            route_id=self.config.keenetic_interface,
            backend_name=BACKEND_NAME,
            changed=changed,
            created=created,
            dry_run=False,
            desired_enabled=feed_snapshot.desired_enabled,
            current_enabled=current_enabled,
            desired_destinations=len(desired_destinations),
            current_destinations=len(current_tokens),
            invalid_entries=feed_snapshot.invalid_count,
            feed_hash=feed_snapshot.feed_hash,
            destinations_hash=feed_snapshot.destinations_hash,
            summary=summary,
            is_blocked=feed_snapshot.is_blocked,
            added_destinations=added_destinations,
            removed_destinations=removed_destinations,
        )

    def _managed_routes(self, routes: list[_RouteEntry]) -> dict[str, _RouteEntry]:
        managed: dict[str, _RouteEntry] = {}
        for route in routes:
            if route.interface != _normalize_text(self.config.keenetic_interface):
                continue
            if route.gateway != _normalize_text(self.config.keenetic_gateway):
                continue
            if route.auto != self.config.keenetic_auto:
                continue
            if route.reject != self.config.keenetic_reject:
                continue
            token = _route_token(route.destination)
            if token in managed:
                raise RemoteRequestError(
                    f"Keenetic managed route set is ambiguous for {token}; adjust the configured interface or gateway"
                )
            managed[token] = route
        return managed
