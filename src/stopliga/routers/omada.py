"""Omada Controller Open API router driver."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Sequence

from ..errors import (
    AuthenticationError,
    DiscoveryError,
    DuplicateRouteError,
    NetworkError,
    PartialUpdateError,
    RemoteRequestError,
    StopLigaError,
    UnsupportedRouteShapeError,
)
from ..logging_utils import log_event
from ..models import Config, FeedSnapshot, SyncResult
from ..utils import make_ssl_context, read_limited, sleep_with_backoff, sort_ip_tokens
from .base import BootstrapGuardClearer, BootstrapGuardWriter, RouterDriver


OMADA_PROTOCOL_ALL = 256
OMADA_GROUP_TYPE_IP = 0
OMADA_SOURCE_TYPE_NETWORK = 0
OMADA_DESTINATION_TYPE_IP_GROUP = 1
OMADA_INTERFACE_TYPE_WAN = 0
OMADA_INTERFACE_TYPE_MULTI = 4
OMADA_MAX_GROUPS = 512
PAGE_SIZE = 1000
SAFE_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
AUTH_ERROR_CODES = frozenset({-44106, -44112, -44113, -44116, -44118})
MANAGED_GROUP_SUFFIX_RE = re.compile(r" \[\d{3}\]$")


@dataclass(frozen=True)
class OmadaSite:
    site_id: str
    name: str


@dataclass(frozen=True)
class OmadaTarget:
    kind: str
    target_id: str
    label: str

    def as_policy_fields(self) -> dict[str, Any]:
        if self.kind == "wan":
            return {
                "interfaceType": OMADA_INTERFACE_TYPE_WAN,
                "interfaceId": self.target_id,
            }
        return {
            "interfaceType": OMADA_INTERFACE_TYPE_MULTI,
            "vpnIds": [self.target_id],
        }


@dataclass(frozen=True)
class GroupMutation:
    action: str
    group_id: str
    payload: dict[str, Any] | None = None


def _normalize_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _sorted_ints(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for item in values:
        if isinstance(item, int):
            result.append(item)
    return sorted(result)


def _collapse_destinations(destinations: Sequence[str]) -> list[str]:
    networks = [ipaddress.ip_network(token, strict=False) for token in destinations]
    if any(network.version != 4 for network in networks):
        raise UnsupportedRouteShapeError("Omada policy routing currently supports IPv4 destinations only")
    ipv4_networks = [network for network in networks if isinstance(network, ipaddress.IPv4Network)]
    return sort_ip_tokens(str(network) for network in ipaddress.collapse_addresses(ipv4_networks))


def _chunked(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _group_destinations(record: dict[str, Any]) -> list[str]:
    entries = record.get("ipList")
    if not isinstance(entries, list):
        return []
    destinations: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ip_value = _normalize_text(entry.get("ip"))
        mask_value = entry.get("mask")
        if ip_value is None or not isinstance(mask_value, int):
            continue
        destinations.append(str(ipaddress.ip_network(f"{ip_value}/{mask_value}", strict=False)))
    return sort_ip_tokens(destinations)


def _group_payload(name: str, destinations: Sequence[str]) -> dict[str, Any]:
    ip_list = []
    for token in destinations:
        network = ipaddress.ip_network(token, strict=False)
        ip_list.append({"ip": str(network.network_address), "mask": network.prefixlen})
    return {
        "name": name,
        "type": OMADA_GROUP_TYPE_IP,
        "ipList": ip_list,
    }


def _route_destination_ids(record: dict[str, Any]) -> list[str]:
    return sorted(_string_list(record.get("destinationIds")))


def _route_source_ids(record: dict[str, Any]) -> list[str]:
    return sorted(_string_list(record.get("sourceIds")))


def _route_status(record: dict[str, Any]) -> bool | None:
    value = record.get("status")
    return value if isinstance(value, bool) else None


def _policy_payload_from_route(record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "name": _normalize_text(record.get("name")) or "",
        "status": bool(record.get("status")),
        "protocols": _sorted_ints(record.get("protocols")),
        "backupInterface": bool(record.get("backupInterface")),
        "sourceType": int(record.get("sourceType", OMADA_SOURCE_TYPE_NETWORK)),
        "sourceIds": _string_list(record.get("sourceIds")),
        "destinationType": int(record.get("destinationType", OMADA_DESTINATION_TYPE_IP_GROUP)),
        "destinationIds": _string_list(record.get("destinationIds")),
    }
    interface_type = record.get("interfaceType")
    if isinstance(interface_type, int):
        payload["interfaceType"] = interface_type
    interface_id = _normalize_text(record.get("interfaceId"))
    if interface_id is not None:
        payload["interfaceId"] = interface_id
    wan_port_ids = _string_list(record.get("wanPortIds") or record.get("wan Port Ids"))
    vpn_ids = _string_list(record.get("vpnIds") or record.get("vpn Ids"))
    if wan_port_ids:
        payload["wanPortIds"] = wan_port_ids
    if vpn_ids:
        payload["vpnIds"] = vpn_ids
    return payload


def _normalize_policy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "name": _normalize_text(payload.get("name")) or "",
        "status": bool(payload.get("status")),
        "protocols": _sorted_ints(payload.get("protocols")),
        "backupInterface": bool(payload.get("backupInterface")),
        "sourceType": int(payload.get("sourceType", OMADA_SOURCE_TYPE_NETWORK)),
        "sourceIds": sorted(_string_list(payload.get("sourceIds"))),
        "destinationType": int(payload.get("destinationType", OMADA_DESTINATION_TYPE_IP_GROUP)),
        "destinationIds": sorted(_string_list(payload.get("destinationIds"))),
        "interfaceType": int(payload.get("interfaceType", OMADA_INTERFACE_TYPE_WAN)),
        "interfaceId": _normalize_text(payload.get("interfaceId")),
        "wanPortIds": sorted(_string_list(payload.get("wanPortIds") or payload.get("wan Port Ids"))),
        "vpnIds": sorted(_string_list(payload.get("vpnIds") or payload.get("vpn Ids"))),
    }
    return normalized


def _flatten_route_destinations(
    route_record: dict[str, Any] | None, groups_by_id: dict[str, dict[str, Any]]
) -> list[str]:
    if route_record is None:
        return []
    if int(route_record.get("destinationType", -1)) != OMADA_DESTINATION_TYPE_IP_GROUP:
        return []
    destinations: list[str] = []
    for group_id in _string_list(route_record.get("destinationIds")):
        group = groups_by_id.get(group_id)
        if group is None:
            continue
        destinations.extend(_group_destinations(group))
    return sort_ip_tokens(destinations)


class OmadaClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = (config.omada_base_url or "").rstrip("/")
        self.context = make_ssl_context(verify=config.omada_verify_tls, ca_file=config.omada_ca_file)
        self.logger = logging.getLogger("stopliga.omada")
        self.access_token: str | None = None

    def authenticate(self) -> None:
        if self.access_token:
            return
        payload = {
            "omadacId": self.config.omada_omadac_id,
            "client_id": self.config.omada_client_id,
            "client_secret": self.config.omada_client_secret,
        }
        url = f"{self.base_url}/openapi/authorize/token?grant_type=client_credentials"
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=self.config.request_timeout) as response:
                raw = read_limited(
                    response,
                    max_bytes=self.config.max_response_bytes,
                    content_length=response.headers.get("Content-Length"),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            message = raw.decode("utf-8", errors="replace") if raw else str(exc)
            raise AuthenticationError(f"Omada authentication failed: HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise NetworkError(f"Omada authentication failed: {exc}") from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RemoteRequestError("Omada authentication returned invalid JSON") from exc
        token = _normalize_text(decoded.get("accessToken")) if isinstance(decoded, dict) else None
        if token is None:
            raise AuthenticationError("Omada authentication did not return an access token")
        self.access_token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: Sequence[int] = (200,),
        retriable: bool = True,
    ) -> Any:
        attempts = self.config.retries if retriable and method in SAFE_RETRY_METHODS else 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            auth_retry_remaining = 1
            while True:
                self.authenticate()
                url = f"{self.base_url}{path}"
                body = json.dumps(json_body, ensure_ascii=True).encode("utf-8") if json_body is not None else None
                headers = {
                    "Accept": "application/json",
                    "Authorization": f"AccessToken={self.access_token}",
                }
                if body is not None:
                    headers["Content-Type"] = "application/json"
                request = urllib.request.Request(url, data=body, method=method, headers=headers)
                try:
                    with urllib.request.urlopen(
                        request, context=self.context, timeout=self.config.request_timeout
                    ) as response:
                        raw = read_limited(
                            response,
                            max_bytes=self.config.max_response_bytes,
                            content_length=response.headers.get("Content-Length"),
                        )
                        status = response.status
                except urllib.error.HTTPError as exc:
                    raw = exc.read()
                    payload = self._decode_json(raw)
                    if exc.code == 401:
                        if auth_retry_remaining > 0:
                            auth_retry_remaining -= 1
                            self.access_token = None
                            log_event(
                                self.logger,
                                logging.INFO,
                                "omada_access_token_retry",
                                method=method,
                                path=path,
                                reason="http_401",
                            )
                            continue
                        raise AuthenticationError("Omada request was rejected with HTTP 401") from exc
                    if payload is not None:
                        try:
                            self._raise_if_api_error(payload)
                        except AuthenticationError:
                            if auth_retry_remaining > 0:
                                auth_retry_remaining -= 1
                                self.access_token = None
                                log_event(
                                    self.logger,
                                    logging.INFO,
                                    "omada_access_token_retry",
                                    method=method,
                                    path=path,
                                    reason="api_auth_error",
                                )
                                continue
                            raise
                    message = raw.decode("utf-8", errors="replace") if raw else str(exc)
                    last_error = RemoteRequestError(
                        f"Omada request failed for {method} {path}: HTTP {exc.code}: {message}"
                    )
                    if attempt < attempts:
                        log_event(
                            self.logger,
                            logging.WARNING,
                            "omada_retry_http",
                            method=method,
                            path=path,
                            attempt=attempt,
                            retries=attempts,
                            error=last_error,
                        )
                        sleep_with_backoff(attempt)
                        break
                    raise last_error from exc
                except urllib.error.URLError as exc:
                    last_error = NetworkError(f"Network failure for {method} {path}: {exc}")
                    if attempt < attempts:
                        log_event(
                            self.logger,
                            logging.WARNING,
                            "omada_retry_network",
                            method=method,
                            path=path,
                            attempt=attempt,
                            retries=attempts,
                            error=last_error,
                        )
                        sleep_with_backoff(attempt)
                        break
                    raise last_error from exc

                if status not in expected_statuses:
                    raise RemoteRequestError(f"Omada request failed for {method} {path}: HTTP {status}")
                if not raw:
                    return {}
                payload = self._decode_json(raw)
                if payload is None:
                    raise RemoteRequestError(f"Omada request for {method} {path} did not return JSON")
                try:
                    self._raise_if_api_error(payload)
                except AuthenticationError:
                    if auth_retry_remaining > 0:
                        auth_retry_remaining -= 1
                        self.access_token = None
                        log_event(
                            self.logger,
                            logging.INFO,
                            "omada_access_token_retry",
                            method=method,
                            path=path,
                            reason="api_auth_error",
                        )
                        continue
                    raise
                return payload

        raise NetworkError(f"Network failure for {method} {path}: {last_error}")

    @staticmethod
    def _decode_json(raw: bytes) -> dict[str, Any] | None:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _raise_if_api_error(payload: dict[str, Any]) -> None:
        error_code = payload.get("errorCode")
        if not isinstance(error_code, int) or error_code == 0:
            return
        message = _normalize_text(payload.get("msg")) or f"errorCode={error_code}"
        if error_code in AUTH_ERROR_CODES:
            raise AuthenticationError(f"Omada API authentication failed: {message}")
        raise RemoteRequestError(f"Omada API returned {message}")

    @staticmethod
    def _records(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise RemoteRequestError("Omada API returned an unsupported response shape")
        result = payload.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        raise RemoteRequestError("Omada API response did not include records")

    @staticmethod
    def _result_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise RemoteRequestError("Omada API returned an unsupported create response shape")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RemoteRequestError("Omada API create response did not include result")
        group_id = _normalize_text(result.get("id"))
        if group_id is None:
            raise RemoteRequestError("Omada API create response did not include an object id")
        return group_id

    def list_sites(self) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"/openapi/v1/{urllib.parse.quote(self.config.omada_omadac_id or '', safe='')}/sites?page=1&pageSize={PAGE_SIZE}",
        )
        return self._records(payload)

    def resolve_site(self) -> OmadaSite:
        target = self.config.site.strip().lower()
        matches: list[OmadaSite] = []
        for record in self.list_sites():
            site_id = _normalize_text(record.get("siteId"))
            name = _normalize_text(record.get("name"))
            aliases = {alias for alias in (site_id, name) if alias is not None}
            if target in {alias.lower() for alias in aliases} and site_id and name:
                matches.append(OmadaSite(site_id=site_id, name=name))
        if not matches:
            raise DiscoveryError(f"Omada site {self.config.site!r} was not found")
        if len(matches) > 1:
            raise DiscoveryError(f"Omada site {self.config.site!r} is ambiguous")
        return matches[0]

    def list_groups(self, site_id: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/profiles/groups")
        return self._records(payload)

    def create_group(self, site_id: str, payload: dict[str, Any]) -> str:
        result = self.request(
            "POST",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/profiles/groups",
            json_body=payload,
            retriable=False,
        )
        return self._result_id(result)

    def update_group(self, site_id: str, group_id: str, payload: dict[str, Any]) -> None:
        self.request(
            "PATCH",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/profiles/groups/{OMADA_GROUP_TYPE_IP}/{group_id}",
            json_body=payload,
            retriable=False,
        )

    def delete_group(self, site_id: str, group_id: str) -> None:
        self.request(
            "DELETE",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/profiles/groups/{OMADA_GROUP_TYPE_IP}/{group_id}",
            retriable=False,
        )

    def list_policy_routes(self, site_id: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/routing/policy-routings?page=1&pageSize={PAGE_SIZE}",
        )
        return self._records(payload)

    def create_policy_route(self, site_id: str, payload: dict[str, Any]) -> None:
        self.request(
            "POST",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/routing/policy-routings",
            json_body=payload,
            retriable=False,
        )

    def update_policy_route(self, site_id: str, route_id: str, payload: dict[str, Any]) -> None:
        self.request(
            "PUT",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/routing/policy-routings/{route_id}",
            json_body=payload,
            retriable=False,
        )

    def delete_policy_route(self, site_id: str, route_id: str) -> None:
        self.request(
            "DELETE",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/routing/policy-routings/{route_id}",
            retriable=False,
        )

    def list_lan_networks(self, site_id: str) -> list[dict[str, Any]]:
        errors: list[str] = []
        for api_version in ("v3", "v2", "v1"):
            try:
                payload = self.request(
                    "GET",
                    f"/openapi/{api_version}/{self.config.omada_omadac_id}/sites/{site_id}/lan-networks?page=1&pageSize={PAGE_SIZE}",
                )
                return self._records(payload)
            except StopLigaError as exc:
                errors.append(f"{api_version}: {exc}")
        raise DiscoveryError("Unable to read Omada LAN networks. " + " | ".join(errors))

    def list_wans(self, site_id: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/qos/gateway/wans")
        return self._records(payload)

    def list_site_to_site_vpns(self, site_id: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET", f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/vpn/site-to-site-vpns"
        )
        return self._records(payload)

    def list_client_to_site_vpns(self, site_id: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET", f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/vpn/client-to-site-vpn-clients"
        )
        return self._records(payload)

    def list_wireguard_vpns(self, site_id: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"/openapi/v1/{self.config.omada_omadac_id}/sites/{site_id}/vpn/wireguards?page=1&pageSize={PAGE_SIZE}",
        )
        return self._records(payload)


class OmadaRouterDriver(RouterDriver):
    router_type = "omada"

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.service")

    def _managed_group_base(self) -> str:
        suffix = " [001]"
        max_base = max(1, 64 - len(suffix))
        return self.config.route_name.strip()[:max_base]

    def _managed_group_name(self, index: int) -> str:
        return f"{self._managed_group_base()} [{index:03d}]"

    def _is_managed_group_name(self, name: str) -> bool:
        base = self._managed_group_base()
        if not name.startswith(base):
            return False
        return bool(MANAGED_GROUP_SUFFIX_RE.search(name))

    def _find_route(self, routes: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        target = self.config.route_name.strip().lower()
        matches = []
        for route in routes:
            route_name = _normalize_text(route.get("name"))
            if route_name is not None and route_name.lower() == target:
                matches.append(route)
        if not matches:
            return None
        if len(matches) > 1:
            raise DuplicateRouteError(f"Omada route {self.config.route_name!r} matched multiple policy routes")
        return matches[0]

    def _build_managed_groups_by_name(self, groups: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        managed: dict[str, dict[str, Any]] = {}
        for group in groups:
            if int(group.get("type", -1)) != OMADA_GROUP_TYPE_IP:
                continue
            name = _normalize_text(group.get("name"))
            group_id = _normalize_text(group.get("groupId"))
            if name is None or group_id is None or not self._is_managed_group_name(name):
                continue
            if name in managed:
                raise DiscoveryError(f"Multiple Omada IP Groups matched the managed name {name!r}")
            managed[name] = group
        return managed

    def _resolve_source_network_ids(self, client: OmadaClient, site_id: str) -> list[str]:
        networks = client.list_lan_networks(site_id)
        if not networks:
            raise DiscoveryError("Omada site does not expose any LAN networks")
        if not self.config.omada_source_networks:
            resolved_network_ids: list[str] = []
            for network in networks:
                network_id = _normalize_text(network.get("id"))
                if network_id:
                    resolved_network_ids.append(network_id)
            if not resolved_network_ids:
                raise DiscoveryError("Omada LAN networks did not expose usable IDs")
            return sorted(set(resolved_network_ids))

        by_alias: dict[str, list[str]] = {}
        for network in networks:
            network_id = _normalize_text(network.get("id"))
            name = _normalize_text(network.get("name"))
            aliases = [alias for alias in (network_id, name) if alias is not None]
            if network_id is None:
                continue
            for alias in aliases:
                by_alias.setdefault(alias.lower(), []).append(network_id)

        resolved_targets: list[str] = []
        for raw_target in self.config.omada_source_networks:
            matched_network_ids = by_alias.get(raw_target.strip().lower(), [])
            if not matched_network_ids:
                raise DiscoveryError(f"Omada source network {raw_target!r} was not found")
            if len(set(matched_network_ids)) > 1:
                raise DiscoveryError(f"Omada source network {raw_target!r} is ambiguous")
            resolved_targets.append(matched_network_ids[0])
        return sorted(set(resolved_targets))

    def _resolve_target(self, client: OmadaClient, site_id: str) -> OmadaTarget:
        configured_target = self.config.omada_target
        if configured_target is None:
            raise DiscoveryError("Omada target is not configured")
        target = configured_target.strip().lower()
        if self.config.omada_target_type == "wan":
            wan_matches: list[OmadaTarget] = []
            for record in client.list_wans(site_id):
                target_id = _normalize_text(record.get("id"))
                name = _normalize_text(record.get("name"))
                aliases = {alias.lower() for alias in (target_id, name) if alias is not None}
                if target in aliases and target_id:
                    wan_matches.append(OmadaTarget(kind="wan", target_id=target_id, label=name or target_id))
            if not wan_matches:
                raise DiscoveryError(f"Omada WAN target {configured_target!r} was not found")
            if len(wan_matches) > 1:
                raise DiscoveryError(f"Omada WAN target {configured_target!r} is ambiguous")
            return wan_matches[0]

        vpn_sources: tuple[tuple[str, Callable[[str], list[dict[str, Any]]]], ...] = (
            ("site-to-site", client.list_site_to_site_vpns),
            ("client-to-site", client.list_client_to_site_vpns),
        )
        for kind, fetch_records in vpn_sources:
            vpn_matches: list[OmadaTarget] = []
            for record in fetch_records(site_id):
                target_id = _normalize_text(record.get("id"))
                name = _normalize_text(record.get("name"))
                aliases = {alias.lower() for alias in (target_id, name) if alias is not None}
                if target in aliases and target_id:
                    label = f"{kind}:{name or target_id}"
                    vpn_matches.append(OmadaTarget(kind="vpn", target_id=target_id, label=label))
            if len(vpn_matches) > 1:
                raise DiscoveryError(f"Omada VPN target {configured_target!r} is ambiguous")
            if vpn_matches:
                return vpn_matches[0]

        # WireGuard list is deprecated in the official docs, so only use it as a fallback.
        wireguard_matches: list[OmadaTarget] = []
        for record in client.list_wireguard_vpns(site_id):
            target_id = _normalize_text(record.get("id"))
            name = _normalize_text(record.get("name"))
            aliases = {alias.lower() for alias in (target_id, name) if alias is not None}
            if target in aliases and target_id:
                label = f"wireguard:{name or target_id}"
                wireguard_matches.append(OmadaTarget(kind="vpn", target_id=target_id, label=label))
        if not wireguard_matches:
            raise DiscoveryError(f"Omada VPN target {configured_target!r} was not found")
        if len(wireguard_matches) > 1:
            raise DiscoveryError(f"Omada VPN target {configured_target!r} is ambiguous")
        return wireguard_matches[0]

    @staticmethod
    def _build_policy_payload(
        *,
        target: OmadaTarget,
        source_ids: Sequence[str],
        destination_ids: Sequence[str],
        enabled: bool,
    ) -> dict[str, Any]:
        payload = {
            "name": "",
            "status": enabled,
            "protocols": [OMADA_PROTOCOL_ALL],
            "backupInterface": False,
            "sourceType": OMADA_SOURCE_TYPE_NETWORK,
            "sourceIds": list(source_ids),
            "destinationType": OMADA_DESTINATION_TYPE_IP_GROUP,
            "destinationIds": list(destination_ids),
        }
        payload.update(target.as_policy_fields())
        return payload

    def _desired_route_payload(
        self,
        *,
        target: OmadaTarget,
        source_ids: Sequence[str],
        destination_ids: Sequence[str],
        enabled: bool,
    ) -> dict[str, Any]:
        payload = self._build_policy_payload(
            target=target,
            source_ids=source_ids,
            destination_ids=destination_ids,
            enabled=enabled,
        )
        payload["name"] = self.config.route_name
        return payload

    def _route_needs_update(self, route_record: dict[str, Any], desired_payload: dict[str, Any]) -> bool:
        return _normalize_policy_payload(_policy_payload_from_route(route_record)) != _normalize_policy_payload(
            desired_payload
        )

    def _rollback_group(self, client: OmadaClient, site_id: str, mutation: GroupMutation) -> None:
        if mutation.action == "delete":
            client.delete_group(site_id, mutation.group_id)
            return
        if mutation.action == "restore" and mutation.payload is not None:
            client.update_group(site_id, mutation.group_id, mutation.payload)

    def _summary(
        self,
        *,
        route_id: str | None,
        target_label: str | None,
        desired_enabled: bool,
        desired_destinations: Sequence[str],
        current_destinations: Sequence[str],
        group_count: int,
    ) -> str:
        target = target_label or "<not-resolved>"
        return (
            f"backend=omada-policy-routing | route={self.config.route_name} | route_id={route_id or '<new>'} | "
            f"target={target} | enabled_desired={desired_enabled} | "
            f"feed_destinations={len(desired_destinations)} | current_destinations={len(current_destinations)} | "
            f"managed_groups={group_count}"
        )

    @staticmethod
    def _destination_delta(current: Sequence[str], desired: Sequence[str]) -> tuple[list[str], list[str]]:
        current_set = set(current)
        desired_set = set(desired)
        added = sort_ip_tokens(desired_set - current_set)
        removed = sort_ip_tokens(current_set - desired_set)
        return added, removed

    def sync(
        self,
        feed_snapshot: FeedSnapshot,
        previous_guard: dict[str, object],
        *,
        guard_writer: BootstrapGuardWriter,
        guard_clearer: BootstrapGuardClearer,
    ) -> SyncResult:
        del previous_guard, guard_writer, guard_clearer

        client = OmadaClient(self.config)
        site = client.resolve_site()
        desired_destinations = _collapse_destinations(feed_snapshot.destinations)

        all_groups = client.list_groups(site.site_id)
        groups_by_id = {
            group_id: group for group in all_groups if (group_id := _normalize_text(group.get("groupId"))) is not None
        }
        managed_groups = self._build_managed_groups_by_name(all_groups)
        managed_group_destinations = {name: _group_destinations(group) for name, group in managed_groups.items()}
        routes = client.list_policy_routes(site.site_id)
        route_record = self._find_route(routes)
        current_destinations = _flatten_route_destinations(route_record, groups_by_id)
        current_enabled = _route_status(route_record) if route_record is not None else None
        if not desired_destinations:
            added_destinations: list[str] = []
            removed_destinations: list[str] = []
            summary = self._summary(
                route_id=_normalize_text(route_record.get("id")) if route_record else None,
                target_label=None,
                desired_enabled=False,
                desired_destinations=desired_destinations,
                current_destinations=current_destinations,
                group_count=len(managed_groups),
            )
            if route_record is None or current_enabled is False:
                return SyncResult(
                    mode="local",
                    route_name=self.config.route_name,
                    route_id=_normalize_text(route_record.get("id")) if route_record else None,
                    backend_name="omada-policy-routing",
                    changed=False,
                    created=False,
                    dry_run=self.config.dry_run,
                    desired_enabled=False,
                    current_enabled=current_enabled,
                    desired_destinations=0,
                    current_destinations=len(current_destinations),
                    invalid_entries=feed_snapshot.invalid_count,
                    feed_hash=feed_snapshot.feed_hash,
                    destinations_hash=feed_snapshot.destinations_hash,
                    summary=summary,
                    is_blocked=feed_snapshot.is_blocked,
                    added_destinations=0,
                    removed_destinations=0,
                )
            desired_route_payload = _policy_payload_from_route(route_record)
            desired_route_payload["status"] = False
            route_changes_needed = self._route_needs_update(route_record, desired_route_payload)
            if self.config.dry_run:
                return SyncResult(
                    mode="local",
                    route_name=self.config.route_name,
                    route_id=_normalize_text(route_record.get("id")),
                    backend_name="omada-policy-routing",
                    changed=route_changes_needed,
                    created=False,
                    dry_run=True,
                    desired_enabled=False,
                    current_enabled=current_enabled,
                    desired_destinations=0,
                    current_destinations=len(current_destinations),
                    invalid_entries=feed_snapshot.invalid_count,
                    feed_hash=feed_snapshot.feed_hash,
                    destinations_hash=feed_snapshot.destinations_hash,
                    summary=summary,
                    is_blocked=feed_snapshot.is_blocked,
                    added_destinations=len(added_destinations),
                    removed_destinations=len(removed_destinations),
                )
            if route_changes_needed:
                disabled_route_id = _normalize_text(route_record.get("id"))
                if disabled_route_id is None:
                    raise RemoteRequestError("Omada route does not expose an id")
                client.update_policy_route(site.site_id, disabled_route_id, desired_route_payload)
                refreshed_route = self._find_route(client.list_policy_routes(site.site_id))
                if refreshed_route is None or self._route_needs_update(refreshed_route, desired_route_payload):
                    raise RemoteRequestError(f"Omada route {self.config.route_name!r} was not disabled")
            return SyncResult(
                mode="local",
                route_name=self.config.route_name,
                route_id=_normalize_text(route_record.get("id")),
                backend_name="omada-policy-routing",
                changed=route_changes_needed,
                created=False,
                dry_run=False,
                desired_enabled=False,
                current_enabled=current_enabled,
                desired_destinations=0,
                current_destinations=len(current_destinations),
                invalid_entries=feed_snapshot.invalid_count,
                feed_hash=feed_snapshot.feed_hash,
                destinations_hash=feed_snapshot.destinations_hash,
                summary=summary,
                is_blocked=feed_snapshot.is_blocked,
                added_destinations=len(added_destinations),
                removed_destinations=len(removed_destinations),
            )

        target = self._resolve_target(client, site.site_id)
        source_ids = self._resolve_source_network_ids(client, site.site_id)
        desired_chunks = _chunked(desired_destinations, self.config.omada_group_size)
        if len(desired_chunks) > OMADA_MAX_GROUPS:
            raise RemoteRequestError(
                f"Omada would require {len(desired_chunks)} IP Groups, which exceeds the conservative safety limit of {OMADA_MAX_GROUPS}"
            )

        desired_group_names = [self._managed_group_name(index) for index in range(1, len(desired_chunks) + 1)]
        reusable_group_ids: list[str] = []
        group_changes_needed = False
        for name, chunk in zip(desired_group_names, desired_chunks, strict=True):
            existing_group = managed_groups.get(name)
            if existing_group is None:
                group_changes_needed = True
                continue
            reusable_group_ids.append(_normalize_text(existing_group.get("groupId")) or "")
            if managed_group_destinations.get(name, []) != chunk:
                group_changes_needed = True

        desired_group_name_set = set(desired_group_names)
        extra_group_ids = sorted(
            group_id
            for name, group in managed_groups.items()
            if name not in desired_group_name_set
            if (group_id := _normalize_text(group.get("groupId"))) is not None
        )
        if extra_group_ids:
            group_changes_needed = True

        desired_route_payload = self._desired_route_payload(
            target=target,
            source_ids=source_ids,
            destination_ids=reusable_group_ids,
            enabled=feed_snapshot.desired_enabled,
        )
        route_changes_needed = route_record is None or len(reusable_group_ids) != len(desired_group_names)
        if route_record is not None and not route_changes_needed:
            route_changes_needed = self._route_needs_update(route_record, desired_route_payload)

        added_destinations, removed_destinations = self._destination_delta(current_destinations, desired_destinations)
        summary = self._summary(
            route_id=_normalize_text(route_record.get("id")) if route_record else None,
            target_label=target.label,
            desired_enabled=feed_snapshot.desired_enabled,
            desired_destinations=desired_destinations,
            current_destinations=current_destinations,
            group_count=len(desired_group_names),
        )

        if self.config.dry_run:
            return SyncResult(
                mode="local",
                route_name=self.config.route_name,
                route_id=_normalize_text(route_record.get("id")) if route_record else None,
                backend_name="omada-policy-routing",
                changed=group_changes_needed or route_changes_needed,
                created=route_record is None,
                dry_run=True,
                desired_enabled=feed_snapshot.desired_enabled,
                current_enabled=current_enabled,
                desired_destinations=len(desired_destinations),
                current_destinations=len(current_destinations),
                invalid_entries=feed_snapshot.invalid_count,
                feed_hash=feed_snapshot.feed_hash,
                destinations_hash=feed_snapshot.destinations_hash,
                summary=summary,
                is_blocked=feed_snapshot.is_blocked,
                added_destinations=len(added_destinations),
                removed_destinations=len(removed_destinations),
            )

        if not group_changes_needed and not route_changes_needed:
            return SyncResult(
                mode="local",
                route_name=self.config.route_name,
                route_id=_normalize_text(route_record.get("id")) if route_record else None,
                backend_name="omada-policy-routing",
                changed=False,
                created=False,
                dry_run=False,
                desired_enabled=feed_snapshot.desired_enabled,
                current_enabled=current_enabled,
                desired_destinations=len(desired_destinations),
                current_destinations=len(current_destinations),
                invalid_entries=feed_snapshot.invalid_count,
                feed_hash=feed_snapshot.feed_hash,
                destinations_hash=feed_snapshot.destinations_hash,
                summary=summary,
                is_blocked=feed_snapshot.is_blocked,
                added_destinations=len(added_destinations),
                removed_destinations=len(removed_destinations),
            )

        group_mutations: list[GroupMutation] = []
        completed_stages: list[str] = []
        current_stage = "prepare"
        created_route = False
        route_changed = False
        route_id: str | None = _normalize_text(route_record.get("id")) if route_record else None
        original_route_payload = _policy_payload_from_route(route_record) if route_record is not None else None
        final_destination_ids: list[str] = []

        try:
            for name, chunk in zip(desired_group_names, desired_chunks, strict=True):
                existing_group = managed_groups.get(name)
                payload = _group_payload(name, chunk)
                if existing_group is not None:
                    group_id = _normalize_text(existing_group.get("groupId"))
                    if group_id is None:
                        raise RemoteRequestError(f"Managed Omada IP Group {name!r} does not expose groupId")
                    final_destination_ids.append(group_id)
                    existing_destinations = managed_group_destinations.get(name, [])
                    if existing_destinations == chunk:
                        continue
                    current_stage = "group-update"
                    client.update_group(site.site_id, group_id, payload)
                    group_mutations.append(
                        GroupMutation("restore", group_id, _group_payload(name, existing_destinations))
                    )
                    completed_stages.append(f"group-update:{name}")
                    continue

                current_stage = "group-create"
                group_id = client.create_group(site.site_id, payload)
                final_destination_ids.append(group_id)
                group_mutations.append(GroupMutation("delete", group_id))
                completed_stages.append(f"group-create:{name}")

            desired_route_payload = self._desired_route_payload(
                target=target,
                source_ids=source_ids,
                destination_ids=final_destination_ids,
                enabled=feed_snapshot.desired_enabled,
            )
            if route_record is None:
                current_stage = "route-create"
                client.create_policy_route(site.site_id, desired_route_payload)
                routes = client.list_policy_routes(site.site_id)
                route_record = self._find_route(routes)
                route_id = _normalize_text(route_record.get("id")) if route_record else None
                if route_record is None or route_id is None:
                    raise RemoteRequestError("Omada route create succeeded but the route could not be found afterwards")
                created_route = True
                route_changed = True
                completed_stages.append("route-create")
            elif self._route_needs_update(route_record, desired_route_payload):
                current_stage = "route-update"
                route_id = _normalize_text(route_record.get("id"))
                if route_id is None:
                    raise RemoteRequestError("Omada route does not expose an id")
                client.update_policy_route(site.site_id, route_id, desired_route_payload)
                route_changed = True
                completed_stages.append("route-update")

            current_stage = "verify"
            refreshed_groups = client.list_groups(site.site_id)
            refreshed_groups_by_id = {
                group_id: group
                for group in refreshed_groups
                if (group_id := _normalize_text(group.get("groupId"))) is not None
            }
            refreshed_routes = client.list_policy_routes(site.site_id)
            refreshed_route = self._find_route(refreshed_routes)
            if refreshed_route is None:
                raise RemoteRequestError(f"Omada route {self.config.route_name!r} disappeared after update")
            if self._route_needs_update(refreshed_route, desired_route_payload):
                raise RemoteRequestError(
                    f"Omada route {self.config.route_name!r} was not updated to the expected state"
                )
            verified_destinations = _flatten_route_destinations(refreshed_route, refreshed_groups_by_id)
            if verified_destinations != desired_destinations:
                raise RemoteRequestError("Omada managed IP Groups were not updated to the expected destination set")

            current_stage = "cleanup"
            deleted_extra_groups = 0
            for group_id in extra_group_ids:
                try:
                    client.delete_group(site.site_id, group_id)
                    deleted_extra_groups += 1
                except StopLigaError as exc:
                    log_event(self.logger, logging.WARNING, "omada_group_cleanup_failed", group_id=group_id, error=exc)

            summary = self._summary(
                route_id=_normalize_text(refreshed_route.get("id")),
                target_label=target.label,
                desired_enabled=feed_snapshot.desired_enabled,
                desired_destinations=desired_destinations,
                current_destinations=current_destinations,
                group_count=len(desired_group_names),
            )
            return SyncResult(
                mode="local",
                route_name=self.config.route_name,
                route_id=_normalize_text(refreshed_route.get("id")),
                backend_name="omada-policy-routing",
                changed=bool(group_mutations or route_changed or deleted_extra_groups),
                created=created_route,
                dry_run=False,
                desired_enabled=feed_snapshot.desired_enabled,
                current_enabled=current_enabled,
                desired_destinations=len(desired_destinations),
                current_destinations=len(current_destinations),
                invalid_entries=feed_snapshot.invalid_count,
                feed_hash=feed_snapshot.feed_hash,
                destinations_hash=feed_snapshot.destinations_hash,
                summary=summary,
                is_blocked=feed_snapshot.is_blocked,
                added_destinations=len(added_destinations),
                removed_destinations=len(removed_destinations),
            )
        except StopLigaError as exc:
            rollback_attempted = False
            rollback_completed = False
            rollback_error: str | None = None
            try:
                rollback_attempted = bool(
                    group_mutations or created_route or (route_changed and route_id and original_route_payload)
                )
                if created_route and route_id:
                    client.delete_policy_route(site.site_id, route_id)
                elif route_changed and route_id and original_route_payload is not None:
                    client.update_policy_route(site.site_id, route_id, original_route_payload)
                for mutation in reversed(group_mutations):
                    self._rollback_group(client, site.site_id, mutation)
                rollback_completed = True
            except StopLigaError as rollback_exc:
                rollback_error = str(rollback_exc)
            raise PartialUpdateError(
                current_stage,
                tuple(completed_stages),
                (
                    f"Omada update failed at stage={current_stage} after completing steps={','.join(completed_stages) or 'none'}: {exc}. "
                    f"rollback_attempted={rollback_attempted} rollback_completed={rollback_completed}"
                    + (f" rollback_error={rollback_error}" if rollback_error else "")
                ),
                rollback_attempted=rollback_attempted,
                rollback_completed=rollback_completed,
                rollback_error=rollback_error,
            ) from exc
