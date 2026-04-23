"""MikroTik RouterOS REST backend."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from ..errors import AuthenticationError, NetworkError, RemoteRequestError
from ..logging_utils import log_event
from ..models import Config, FeedSnapshot, SyncResult
from ..utils import canonicalize_ip_token, make_ssl_context, read_limited, sleep_with_backoff, sort_ip_tokens
from .base import BootstrapGuardClearer, BootstrapGuardWriter, RouterDriver


BACKEND_NAME = "mikrotik-address-list-routing"


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_flag(value: Any) -> bool:
    lowered = str(value).strip().lower()
    return lowered in {"true", "yes", "on", "1"}


def _managed_comment(config: Config, kind: str) -> str:
    return f"stopliga:{kind}:{config.route_name.strip()}"


def _managed_address_list_name(config: Config) -> str:
    configured = _normalize_text(config.mikrotik_address_list)
    if configured is not None:
        return configured
    return config.route_name.strip()


def _quoted_id(item_id: str) -> str:
    return urllib.parse.quote(item_id, safe="*")


def _error_message(payload: dict[str, Any] | None, *, fallback: str) -> str:
    if not payload:
        return fallback
    pieces = [
        _normalize_text(payload.get("message")),
        _normalize_text(payload.get("detail")),
        _normalize_text(payload.get("error")),
    ]
    detail = " | ".join(piece for piece in pieces if piece)
    return detail or fallback


@dataclass(frozen=True)
class _DiscoverySnapshot:
    table_id: str | None
    route_id: str | None
    rule_id: str | None
    current_enabled: bool | None
    current_destinations: list[str]


class MikroTikClient:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.mikrotik")
        self.base_url = f"https://{self._format_host(config.host or '')}:{config.port}/rest"
        auth = f"{config.mikrotik_username or ''}:{config.mikrotik_password or ''}".encode("utf-8")
        self.authorization = "Basic " + base64.b64encode(auth).decode("ascii")
        context = make_ssl_context(verify=config.mikrotik_verify_tls, ca_file=config.mikrotik_ca_file)
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    @staticmethod
    def _format_host(host: str) -> str:
        if ":" in host and not host.startswith("["):
            return f"[{host}]"
        return host

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
        url = f"{self.base_url}/{path.lstrip('/')}{encoded_query}"
        headers = {
            "Accept": "application/json",
            "Authorization": self.authorization,
        }
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
                payload_data: dict[str, Any] | None = None
                raw = exc.read()
                if raw:
                    try:
                        decoded = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        decoded = None
                    if isinstance(decoded, dict):
                        payload_data = decoded
                if exc.code in {401, 403}:
                    raise AuthenticationError(
                        _error_message(payload_data, fallback="MikroTik authentication failed")
                    ) from exc
                if exc.code >= 500 and attempt < self.config.retries:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "mikrotik_retry_http",
                        attempt=attempt,
                        status=exc.code,
                        path=path,
                    )
                    sleep_with_backoff(attempt)
                    continue
                message = _error_message(payload_data, fallback=f"MikroTik REST request failed with HTTP {exc.code}")
                raise RemoteRequestError(f"{method.upper()} {path}: {message}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.retries:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "mikrotik_retry_network",
                        attempt=attempt,
                        path=path,
                        error=exc,
                    )
                    sleep_with_backoff(attempt)
                    continue
                raise NetworkError(f"MikroTik REST request failed for {method.upper()} {path}: {exc}") from exc
        raise NetworkError(f"MikroTik REST request failed for {method.upper()} {path}: {last_error}")

    def list_items(self, path: str, *, query: dict[str, str] | None = None) -> list[dict[str, Any]]:
        payload = self.request("GET", path, query=query)
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise RemoteRequestError(f"MikroTik REST GET {path} returned an unexpected payload")
        return [item for item in payload if isinstance(item, dict)]

    def create_item(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        created = self.request("PUT", path, payload=payload)
        if not isinstance(created, dict):
            raise RemoteRequestError(f"MikroTik REST PUT {path} did not return the created object")
        return created

    def update_item(self, path: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        updated = self.request("PATCH", f"{path}/{_quoted_id(item_id)}", payload=payload)
        if not isinstance(updated, dict):
            raise RemoteRequestError(f"MikroTik REST PATCH {path}/{item_id} did not return the updated object")
        return updated

    def delete_item(self, path: str, item_id: str) -> None:
        self.request("DELETE", f"{path}/{_quoted_id(item_id)}")


@dataclass(frozen=True)
class MikroTikRouterDriver(RouterDriver):
    config: Config
    router_type: str = "mikrotik"

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger("stopliga.mikrotik")

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

        client = MikroTikClient(self.config)
        list_name = _managed_address_list_name(self.config)
        table_name = (self.config.mikrotik_routing_table or "").strip()
        gateway = (self.config.mikrotik_gateway or "").strip()
        desired_destinations = sort_ip_tokens(feed_snapshot.destinations)

        discovery = self._discover(client, list_name=list_name, table_name=table_name)
        added_destinations = len(
            [token for token in desired_destinations if token not in discovery.current_destinations]
        )
        removed_destinations = len(
            [token for token in discovery.current_destinations if token not in desired_destinations]
        )

        route_changes_needed = discovery.table_id is None or discovery.route_id is None
        if discovery.route_id is not None:
            route_record = self._find_managed_route(client, table_name=table_name)
            route_changes_needed = self._route_needs_update(route_record, table_name=table_name, gateway=gateway)
        rule_changes_needed = discovery.rule_id is None
        if discovery.rule_id is not None:
            rule_record = self._find_managed_rule(client)
            rule_changes_needed = self._rule_needs_update(
                rule_record, list_name=list_name, desired_enabled=feed_snapshot.desired_enabled
            )
        address_list_changes_needed = desired_destinations != discovery.current_destinations

        summary = (
            f"list={list_name} table={table_name} enabled={feed_snapshot.desired_enabled} "
            f"ips={len(desired_destinations)} added={added_destinations} removed={removed_destinations}"
        )

        log_event(
            self.logger,
            logging.INFO,
            "mikrotik_state_check",
            backend=BACKEND_NAME,
            table=table_name,
            list_name=list_name,
            current_enabled=discovery.current_enabled,
            desired_enabled=feed_snapshot.desired_enabled,
            current_destinations=len(discovery.current_destinations),
            desired_destinations=len(desired_destinations),
            route_present=discovery.route_id is not None,
            rule_present=discovery.rule_id is not None,
        )

        if self.config.dry_run:
            return SyncResult(
                mode="mikrotik",
                route_name=self.config.route_name,
                route_id=discovery.rule_id,
                backend_name=BACKEND_NAME,
                changed=address_list_changes_needed or route_changes_needed or rule_changes_needed,
                created=discovery.route_id is None or discovery.rule_id is None or discovery.table_id is None,
                dry_run=True,
                desired_enabled=feed_snapshot.desired_enabled,
                current_enabled=discovery.current_enabled,
                desired_destinations=len(desired_destinations),
                current_destinations=len(discovery.current_destinations),
                invalid_entries=feed_snapshot.invalid_count,
                feed_hash=feed_snapshot.feed_hash,
                destinations_hash=feed_snapshot.destinations_hash,
                summary=summary,
                is_blocked=feed_snapshot.is_blocked,
                added_destinations=added_destinations,
                removed_destinations=removed_destinations,
            )

        table_record, table_created = self._ensure_table(client, table_name=table_name)
        route_record, route_created, route_changed = self._ensure_route(
            client,
            table_name=table_name,
            gateway=gateway,
        )
        current_destinations, list_changed = self._reconcile_address_list(
            client,
            list_name=list_name,
            desired_destinations=desired_destinations,
        )
        rule_record, rule_created, rule_changed = self._ensure_rule(
            client,
            list_name=list_name,
            desired_enabled=feed_snapshot.desired_enabled,
        )

        changed = table_created or route_created or route_changed or list_changed or rule_created or rule_changed
        created = table_created or route_created or rule_created
        return SyncResult(
            mode="mikrotik",
            route_name=self.config.route_name,
            route_id=_normalize_text(rule_record.get(".id")),
            backend_name=BACKEND_NAME,
            changed=changed,
            created=created,
            dry_run=False,
            desired_enabled=feed_snapshot.desired_enabled,
            current_enabled=discovery.current_enabled,
            desired_destinations=len(desired_destinations),
            current_destinations=len(discovery.current_destinations),
            invalid_entries=feed_snapshot.invalid_count,
            feed_hash=feed_snapshot.feed_hash,
            destinations_hash=feed_snapshot.destinations_hash,
            summary=summary,
            is_blocked=feed_snapshot.is_blocked,
            added_destinations=added_destinations,
            removed_destinations=removed_destinations,
        )

    def _discover(self, client: MikroTikClient, *, list_name: str, table_name: str) -> _DiscoverySnapshot:
        table_record = self._find_unique(
            client.list_items("routing/table", query={"name": table_name}),
            label=f"MikroTik routing table {table_name!r}",
        )
        route_record = self._find_managed_route(client, table_name=table_name)
        rule_record = self._find_managed_rule(client)
        address_entries = client.list_items("ip/firewall/address-list", query={"list": list_name})
        current_destinations = self._normalized_address_list_entries(address_entries)
        return _DiscoverySnapshot(
            table_id=_normalize_text(table_record.get(".id")) if table_record is not None else None,
            route_id=_normalize_text(route_record.get(".id")) if route_record is not None else None,
            rule_id=_normalize_text(rule_record.get(".id")) if rule_record is not None else None,
            current_enabled=None if rule_record is None else not _normalize_flag(rule_record.get("disabled")),
            current_destinations=current_destinations,
        )

    def _find_managed_route(self, client: MikroTikClient, *, table_name: str) -> dict[str, Any] | None:
        records = client.list_items(
            "ip/route",
            query={
                "comment": _managed_comment(self.config, "route"),
                "routing-table": table_name,
                "dst-address": "0.0.0.0/0",
            },
        )
        return self._find_unique(records, label="MikroTik managed route")

    def _find_managed_rule(self, client: MikroTikClient) -> dict[str, Any] | None:
        records = client.list_items(
            "ip/firewall/mangle",
            query={"comment": _managed_comment(self.config, "mangle")},
        )
        return self._find_unique(records, label="MikroTik managed mangle rule")

    @staticmethod
    def _find_unique(records: list[dict[str, Any]], *, label: str) -> dict[str, Any] | None:
        if not records:
            return None
        if len(records) > 1:
            raise RemoteRequestError(
                f"{label} is ambiguous; expected exactly one managed object and found {len(records)}"
            )
        return records[0]

    def _table_payload(self, *, table_name: str) -> dict[str, Any]:
        return {"name": table_name, "fib": True}

    def _route_payload(self, *, table_name: str, gateway: str) -> dict[str, Any]:
        return {
            "comment": _managed_comment(self.config, "route"),
            "disabled": False,
            "distance": self.config.mikrotik_route_distance,
            "dst-address": "0.0.0.0/0",
            "gateway": gateway,
            "routing-table": table_name,
        }

    def _rule_payload(self, *, list_name: str, desired_enabled: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "comment": _managed_comment(self.config, "mangle"),
            "chain": "prerouting",
            "action": "mark-routing",
            "disabled": not desired_enabled,
            "dst-address-list": list_name,
            "dst-address-type": "!local",
            "new-routing-mark": (self.config.mikrotik_routing_table or "").strip(),
            "passthrough": False,
        }
        if self.config.mikrotik_in_interface:
            payload["in-interface"] = self.config.mikrotik_in_interface
        if self.config.mikrotik_in_interface_list:
            payload["in-interface-list"] = self.config.mikrotik_in_interface_list
        return payload

    def _ensure_table(self, client: MikroTikClient, *, table_name: str) -> tuple[dict[str, Any], bool]:
        existing = self._find_unique(
            client.list_items("routing/table", query={"name": table_name}),
            label=f"MikroTik routing table {table_name!r}",
        )
        if existing is not None:
            return existing, False
        created = client.create_item("routing/table", self._table_payload(table_name=table_name))
        log_event(self.logger, logging.INFO, "mikrotik_table_created", table=table_name, table_id=created.get(".id"))
        return created, True

    def _route_needs_update(self, route_record: dict[str, Any] | None, *, table_name: str, gateway: str) -> bool:
        if route_record is None:
            return True
        expected = self._route_payload(table_name=table_name, gateway=gateway)
        for key, value in expected.items():
            current_value = route_record.get(key)
            if isinstance(value, bool):
                if _normalize_flag(current_value) != value:
                    return True
                continue
            if _normalize_text(current_value) != str(value):
                return True
        return False

    def _ensure_route(
        self, client: MikroTikClient, *, table_name: str, gateway: str
    ) -> tuple[dict[str, Any], bool, bool]:
        existing = self._find_managed_route(client, table_name=table_name)
        payload = self._route_payload(table_name=table_name, gateway=gateway)
        if existing is None:
            created = client.create_item("ip/route", payload)
            log_event(
                self.logger, logging.INFO, "mikrotik_route_created", route_id=created.get(".id"), table=table_name
            )
            return created, True, True
        route_id = _normalize_text(existing.get(".id"))
        if route_id is None:
            raise RemoteRequestError("MikroTik managed route does not expose an .id")
        if not self._route_needs_update(existing, table_name=table_name, gateway=gateway):
            return existing, False, False
        updated = client.update_item("ip/route", route_id, payload)
        log_event(self.logger, logging.INFO, "mikrotik_route_updated", route_id=route_id, table=table_name)
        return updated, False, True

    def _rule_needs_update(self, rule_record: dict[str, Any] | None, *, list_name: str, desired_enabled: bool) -> bool:
        if rule_record is None:
            return True
        expected = self._rule_payload(list_name=list_name, desired_enabled=desired_enabled)
        for key, value in expected.items():
            current_value = rule_record.get(key)
            if isinstance(value, bool):
                if _normalize_flag(current_value) != value:
                    return True
                continue
            if _normalize_text(current_value) != str(value):
                return True
        return False

    def _ensure_rule(
        self,
        client: MikroTikClient,
        *,
        list_name: str,
        desired_enabled: bool,
    ) -> tuple[dict[str, Any], bool, bool]:
        existing = self._find_managed_rule(client)
        payload = self._rule_payload(list_name=list_name, desired_enabled=desired_enabled)
        if existing is None:
            created = client.create_item("ip/firewall/mangle", payload)
            log_event(self.logger, logging.INFO, "mikrotik_rule_created", rule_id=created.get(".id"))
            return created, True, True
        rule_id = _normalize_text(existing.get(".id"))
        if rule_id is None:
            raise RemoteRequestError("MikroTik managed mangle rule does not expose an .id")
        if not self._rule_needs_update(existing, list_name=list_name, desired_enabled=desired_enabled):
            return existing, False, False
        updated = client.update_item("ip/firewall/mangle", rule_id, payload)
        log_event(self.logger, logging.INFO, "mikrotik_rule_updated", rule_id=rule_id, enabled=desired_enabled)
        return updated, False, True

    def _normalized_address_list_entries(self, entries: list[dict[str, Any]]) -> list[str]:
        normalized: list[str] = []
        for entry in entries:
            address = _normalize_text(entry.get("address"))
            if address is None:
                raise RemoteRequestError("MikroTik address-list entry is missing its address")
            try:
                normalized.append(canonicalize_ip_token(address))
            except ValueError as exc:
                raise RemoteRequestError(f"MikroTik address-list entry has an invalid address: {address!r}") from exc
        return sort_ip_tokens(normalized)

    def _reconcile_address_list(
        self,
        client: MikroTikClient,
        *,
        list_name: str,
        desired_destinations: list[str],
    ) -> tuple[list[str], bool]:
        entries = client.list_items("ip/firewall/address-list", query={"list": list_name})
        entries_by_token: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            address = _normalize_text(entry.get("address"))
            if address is None:
                raise RemoteRequestError("MikroTik address-list entry is missing its address")
            token = canonicalize_ip_token(address)
            entries_by_token.setdefault(token, []).append(entry)

        current_destinations = sort_ip_tokens(entries_by_token)
        desired_set = set(desired_destinations)
        changed = False

        for token, token_entries in entries_by_token.items():
            if token in desired_set and len(token_entries) == 1:
                continue
            keep_first = token in desired_set
            stale_entries = token_entries[1:] if keep_first else token_entries
            for entry in stale_entries:
                item_id = _normalize_text(entry.get(".id"))
                if item_id is None:
                    raise RemoteRequestError("MikroTik address-list entry does not expose an .id")
                client.delete_item("ip/firewall/address-list", item_id)
                changed = True

        current_set = set(current_destinations)
        for token in desired_destinations:
            if token in current_set:
                continue
            client.create_item(
                "ip/firewall/address-list",
                {
                    "address": token,
                    "comment": _managed_comment(self.config, "destination"),
                    "list": list_name,
                },
            )
            changed = True

        if changed:
            refreshed = client.list_items("ip/firewall/address-list", query={"list": list_name})
            return self._normalized_address_list_entries(refreshed), True
        return current_destinations, False
