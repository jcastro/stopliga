"""FRITZ!Box TR-064 backend."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
from typing import Any
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from ..errors import AuthenticationError, DiscoveryError, NetworkError, RemoteRequestError
from ..logging_utils import log_event
from ..models import Config, FeedSnapshot, SyncResult
from ..utils import make_ssl_context, read_limited, sleep_with_backoff
from .base import BootstrapGuardClearer, BootstrapGuardWriter, RouterDriver


BACKEND_NAME = "fritzbox-static-routes"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICE_TYPE = "urn:dslforum-org:service:Layer3Forwarding:1"
SERVICE_ID = "urn:Layer3Forwarding-com:serviceId:Layer3Forwarding1"


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _route_token(network: ipaddress.IPv4Network) -> str:
    return str(network)


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
            raise RemoteRequestError("FRITZ!Box backend currently supports only IPv4 destinations")
        networks.append(network)
    collapsed = ipaddress.collapse_addresses(networks)
    return sorted(
        (network for network in collapsed if isinstance(network, ipaddress.IPv4Network)),
        key=lambda network: (int(network.network_address), network.prefixlen),
    )


@dataclass(frozen=True)
class _ServiceDescription:
    prefix: str
    control_url: str
    service_type: str


@dataclass(frozen=True)
class _ForwardingEntry:
    destination: ipaddress.IPv4Network
    gateway: str
    interface: str
    route_type: str
    metric: int
    enabled: bool
    source_ip: str
    source_mask: str


class FRITZBoxClient:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.fritzbox")
        self.base_url = f"https://{self._format_host(config.host or '')}:{config.port}"
        self.service: _ServiceDescription | None = None
        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            self.base_url,
            config.fritzbox_username or "",
            config.fritzbox_password or "",
        )
        context = make_ssl_context(verify=config.fritzbox_verify_tls, ca_file=config.fritzbox_ca_file)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(password_manager),
            urllib.request.HTTPSHandler(context=context),
        )

    @staticmethod
    def _format_host(host: str) -> str:
        if ":" in host and not host.startswith("["):
            return f"[{host}]"
        return host

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> bytes | None:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        last_error: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            try:
                with self.opener.open(request, timeout=self.config.request_timeout) as response:
                    return read_limited(
                        response,
                        max_bytes=self.config.max_response_bytes,
                        content_length=response.headers.get("Content-Length"),
                    )
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if allow_not_found and exc.code == 404:
                    return None
                if exc.code in {401, 403}:
                    raise AuthenticationError("FRITZ!Box authentication failed") from exc
                if exc.code >= 500 and attempt < self.config.retries:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "fritzbox_retry_http",
                        attempt=attempt,
                        status=exc.code,
                        path=path,
                    )
                    sleep_with_backoff(attempt)
                    continue
                details = raw.decode("utf-8", errors="replace") if raw else f"HTTP {exc.code}"
                raise RemoteRequestError(f"FRITZ!Box request {method} {path} failed: {details}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if attempt < self.config.retries:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "fritzbox_retry_network",
                        attempt=attempt,
                        path=path,
                        error=exc,
                    )
                    sleep_with_backoff(attempt)
                    continue
                raise NetworkError(f"FRITZ!Box request {method} {path} failed: {exc}") from exc
        raise NetworkError(f"FRITZ!Box request {method} {path} failed: {last_error}")

    def discover_service(self) -> _ServiceDescription:
        if self.service is not None:
            return self.service

        for prefix in ("", "/tr064"):
            body = self._request("GET", f"{prefix}/tr64desc.xml", allow_not_found=True)
            if body is None:
                continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise DiscoveryError("FRITZ!Box root service description is not valid XML") from exc
            service = self._find_service(root, prefix)
            if service is not None:
                self.service = service
                return service
        raise DiscoveryError("FRITZ!Box TR-064 Layer3Forwarding service was not found in tr64desc.xml")

    def _find_service(self, root: ET.Element, prefix: str) -> _ServiceDescription | None:
        for service in root.iter():
            if _tag_name(service.tag) != "service":
                continue
            values = {_tag_name(child.tag): (child.text or "").strip() for child in service}
            service_type = values.get("serviceType")
            service_id = values.get("serviceId")
            control_url = values.get("controlURL")
            if not control_url:
                continue
            if service_id == SERVICE_ID or service_type == SERVICE_TYPE:
                return _ServiceDescription(
                    prefix=prefix, control_url=control_url, service_type=service_type or SERVICE_TYPE
                )
        return None

    def _soap_call(self, action: str, arguments: dict[str, str] | None = None) -> dict[str, str]:
        service = self.discover_service()
        envelope = ET.Element(f"{{{SOAP_ENV_NS}}}Envelope")
        envelope.set("encodingStyle", "http://schemas.xmlsoap.org/soap/encoding/")
        body = ET.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
        action_element = ET.SubElement(body, f"{{{service.service_type}}}{action}")
        for name, value in (arguments or {}).items():
            child = ET.SubElement(action_element, name)
            child.text = value
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        control_url = service.control_url
        if service.prefix and not control_url.startswith(service.prefix):
            control_url = f"{service.prefix}{control_url}"
        raw = self._request(
            "POST",
            control_url,
            body=payload,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SoapAction": f'"{service.service_type}#{action}"',
            },
        )
        if raw is None:
            return {}
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise RemoteRequestError(f"FRITZ!Box SOAP response for {action} is not valid XML") from exc

        for fault in root.iter():
            if _tag_name(fault.tag) != "Fault":
                continue
            pieces = []
            for child in fault.iter():
                if _tag_name(child.tag) in {"faultstring", "errorCode", "errorDescription"} and child.text:
                    pieces.append(child.text.strip())
            message = " | ".join(piece for piece in pieces if piece) or f"SOAP fault in {action}"
            raise RemoteRequestError(f"FRITZ!Box SOAP action {action} failed: {message}")

        response_values: dict[str, str] = {}
        response_element_name = f"{action}Response"
        for element in root.iter():
            if _tag_name(element.tag) != response_element_name:
                continue
            for child in element:
                response_values[_tag_name(child.tag)] = (child.text or "").strip()
            return response_values
        return response_values

    def get_default_connection_service(self) -> str:
        response = self._soap_call("GetDefaultConnectionService")
        value = _normalize_text(response.get("NewDefaultConnectionService"))
        if value is None:
            raise RemoteRequestError("FRITZ!Box did not return DefaultConnectionService")
        return value

    def list_forwarding_entries(self) -> list[_ForwardingEntry]:
        response = self._soap_call("GetForwardNumberOfEntries")
        raw_count = _normalize_text(response.get("NewForwardNumberOfEntries"))
        try:
            count = int(raw_count or "0")
        except ValueError as exc:
            raise RemoteRequestError("FRITZ!Box did not return a valid forwarding entry count") from exc

        entries: list[_ForwardingEntry] = []
        for index in range(count):
            payload = self._soap_call("GetGenericForwardingEntry", {"NewForwardingIndex": str(index)})
            destination = ipaddress.ip_network(
                f"{payload.get('NewDestIPAddress', '')}/{payload.get('NewDestSubnetMask', '')}",
                strict=False,
            )
            if not isinstance(destination, ipaddress.IPv4Network):
                continue
            metric = int(payload.get("NewForwardingMetric", "0") or "0")
            entries.append(
                _ForwardingEntry(
                    destination=destination,
                    gateway=payload.get("NewGatewayIPAddress", ""),
                    interface=payload.get("NewInterface", ""),
                    route_type=payload.get("NewType", ""),
                    metric=metric,
                    enabled=_truthy(payload.get("NewEnable", "")),
                    source_ip=payload.get("NewSourceIPAddress", ""),
                    source_mask=payload.get("NewSourceSubnetMask", ""),
                )
            )
        return entries

    def add_forwarding_entry(
        self,
        destination: ipaddress.IPv4Network,
        *,
        gateway: str,
        interface: str,
        metric: int,
    ) -> None:
        self._soap_call(
            "AddForwardingEntry",
            {
                "NewType": "Host" if destination.prefixlen == 32 else "Network",
                "NewDestIPAddress": str(destination.network_address),
                "NewDestSubnetMask": str(destination.netmask),
                "NewSourceIPAddress": "0.0.0.0",
                "NewSourceSubnetMask": "0.0.0.0",
                "NewGatewayIPAddress": gateway,
                "NewInterface": interface,
                "NewForwardingMetric": str(metric),
            },
        )

    def delete_forwarding_entry(self, destination: ipaddress.IPv4Network) -> None:
        self._soap_call(
            "DeleteForwardingEntry",
            {
                "NewDestIPAddress": str(destination.network_address),
                "NewDestSubnetMask": str(destination.netmask),
                "NewSourceIPAddress": "0.0.0.0",
                "NewSourceSubnetMask": "0.0.0.0",
            },
        )

    def set_forwarding_entry_enabled(self, destination: ipaddress.IPv4Network, enabled: bool) -> None:
        self._soap_call(
            "SetForwardingEntryEnable",
            {
                "NewDestIPAddress": str(destination.network_address),
                "NewDestSubnetMask": str(destination.netmask),
                "NewSourceIPAddress": "0.0.0.0",
                "NewSourceSubnetMask": "0.0.0.0",
                "NewEnable": "1" if enabled else "0",
            },
        )


@dataclass(frozen=True)
class FRITZBoxRouterDriver(RouterDriver):
    config: Config
    router_type: str = "fritzbox"

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger("stopliga.fritzbox")

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

        client = FRITZBoxClient(self.config)
        desired_destinations = _collapse_destinations(feed_snapshot.destinations)
        current_entries = client.list_forwarding_entries()
        managed_entries = self._managed_entries(current_entries)
        current_destinations = sorted(managed_entries)

        desired_tokens = {_route_token(network) for network in desired_destinations}
        current_tokens = set(current_destinations)
        added_destinations = len(desired_tokens - current_tokens)
        removed_destinations = len(current_tokens - desired_tokens)
        toggles_needed = any(entry.enabled != feed_snapshot.desired_enabled for entry in managed_entries.values())
        changed = added_destinations > 0 or removed_destinations > 0 or toggles_needed

        summary = (
            f"gateway={self.config.fritzbox_gateway} metric={self.config.fritzbox_route_metric} "
            f"enabled={feed_snapshot.desired_enabled} routes={len(desired_destinations)} "
            f"added={added_destinations} removed={removed_destinations}"
        )
        current_enabled = None
        if managed_entries:
            current_enabled = all(entry.enabled for entry in managed_entries.values())

        log_event(
            self.logger,
            logging.INFO,
            "fritzbox_route_check",
            backend=BACKEND_NAME,
            current_destinations=len(current_destinations),
            desired_destinations=len(desired_destinations),
            current_enabled=current_enabled,
            desired_enabled=feed_snapshot.desired_enabled,
            metric=self.config.fritzbox_route_metric,
        )

        if self.config.dry_run:
            return SyncResult(
                mode="fritzbox",
                route_name=self.config.route_name,
                route_id=self.config.host,
                backend_name=BACKEND_NAME,
                changed=changed,
                created=added_destinations > 0,
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
                added_destinations=added_destinations,
                removed_destinations=removed_destinations,
            )

        default_connection_service = client.get_default_connection_service()
        created = False

        for destination in desired_destinations:
            token = _route_token(destination)
            if token in managed_entries:
                continue
            client.add_forwarding_entry(
                destination,
                gateway=self.config.fritzbox_gateway or "",
                interface=default_connection_service,
                metric=self.config.fritzbox_route_metric,
            )
            created = True
            if not feed_snapshot.desired_enabled:
                client.set_forwarding_entry_enabled(destination, False)

        for token, entry in managed_entries.items():
            if token in desired_tokens:
                if entry.enabled != feed_snapshot.desired_enabled:
                    client.set_forwarding_entry_enabled(entry.destination, feed_snapshot.desired_enabled)
                continue
            client.delete_forwarding_entry(entry.destination)

        refreshed_entries = self._managed_entries(client.list_forwarding_entries())
        refreshed_tokens = sorted(refreshed_entries)
        if set(refreshed_tokens) != desired_tokens:
            raise RemoteRequestError("FRITZ!Box routes were not reconciled to the expected destination set")
        if refreshed_entries and any(
            entry.enabled != feed_snapshot.desired_enabled for entry in refreshed_entries.values()
        ):
            raise RemoteRequestError("FRITZ!Box routes were not updated to the expected enabled state")

        return SyncResult(
            mode="fritzbox",
            route_name=self.config.route_name,
            route_id=self.config.host,
            backend_name=BACKEND_NAME,
            changed=changed,
            created=created,
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
            added_destinations=added_destinations,
            removed_destinations=removed_destinations,
        )

    def _managed_entries(self, entries: list[_ForwardingEntry]) -> dict[str, _ForwardingEntry]:
        managed: dict[str, _ForwardingEntry] = {}
        for entry in entries:
            if entry.gateway != (self.config.fritzbox_gateway or ""):
                continue
            if entry.metric != self.config.fritzbox_route_metric:
                continue
            if entry.source_ip != "0.0.0.0" or entry.source_mask != "0.0.0.0":
                continue
            token = _route_token(entry.destination)
            if token in managed:
                raise RemoteRequestError(
                    f"FRITZ!Box managed route set is ambiguous for {token}; use a unique FRITZBOX_ROUTE_METRIC"
                )
            managed[token] = entry
        return managed
