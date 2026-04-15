"""UniFi local client, backend selection and route synchronization logic."""

from __future__ import annotations

import copy
import http.cookiejar
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Iterable, Sequence

from .errors import AuthenticationError, ConfigError, DiscoveryError, DuplicateRouteError, NetworkError, RemoteRequestError, RouteNotFoundError, StopLigaError, UnsupportedRouteShapeError
from .logging_utils import log_event
from .models import BootstrapPreview, Config, FeedSnapshot, SiteContext, UpdatePlan
from .utils import canonicalize_ip_token, make_ssl_context, shorten_json, sleep_with_backoff, sort_ip_tokens


MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:-]?[0-9a-fA-F]{2}){5}$")


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """Extract a list of dictionaries from common API response shapes."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "sites"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def site_aliases(record: dict[str, Any]) -> set[str]:
    """Return a normalized set of site aliases that can be used for matching."""

    aliases: set[str] = set()
    for key in (
        "name",
        "desc",
        "description",
        "displayName",
        "internalReference",
        "internal_reference",
        "_id",
        "id",
        "site_id",
        "siteId",
        "hostId",
        "ipAddress",
        "hardwareId",
    ):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            aliases.add(value.strip().lower())
    meta = record.get("meta")
    if isinstance(meta, dict):
        for key in ("name", "desc", "description", "gatewayMac"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                aliases.add(value.strip().lower())
    reported_state = record.get("reportedState")
    if isinstance(reported_state, dict):
        for key in ("name", "hostName", "ipAddress"):
            value = reported_state.get(key)
            if isinstance(value, str) and value.strip():
                aliases.add(value.strip().lower())
    return aliases


def pick_site_internal_name(record: dict[str, Any]) -> str | None:
    """Pick the most likely UniFi Network internal site name."""

    for key in ("name", "internalReference", "internal_reference", "desc", "description"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = record.get("meta")
    if isinstance(meta, dict):
        for key in ("name", "desc", "description"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def pick_site_id(record: dict[str, Any]) -> str | None:
    """Pick the most likely stable site identifier."""

    for key in ("siteId", "id", "site_id", "_id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def find_records(records: Sequence[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    target_lower = target.strip().lower()
    if not target_lower:
        return []
    return [record for record in records if target_lower in site_aliases(record)]


def select_record(records: Sequence[dict[str, Any]], target: str) -> dict[str, Any] | None:
    matches = find_records(records, target)
    if not matches:
        return None
    if len(matches) > 1:
        raise DiscoveryError(f"Ambiguous site identifier {target!r}; multiple records matched")
    return matches[0]


def match_record(records: Sequence[dict[str, Any]], aliases: set[str]) -> dict[str, Any] | None:
    for record in records:
        if aliases & site_aliases(record):
            return record
    return None


def route_label(route: dict[str, Any]) -> str:
    """Choose a human-readable route label for logs."""

    for key in ("name", "description", "desc", "_id", "id"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "<unnamed>"


def route_id(route: dict[str, Any]) -> str:
    """Return the route identifier."""

    value = route.get("_id") or route.get("id")
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedRouteShapeError("Route payload does not expose _id/id")
    return value.strip()


def find_matching_routes(routes: Sequence[dict[str, Any]], target_name: str) -> list[dict[str, Any]]:
    """Return all exact matches for the target route name."""

    target = target_name.strip().lower()
    matches: list[dict[str, Any]] = []
    for route in routes:
        for key in ("name", "description", "desc"):
            value = route.get(key)
            if isinstance(value, str) and value.strip().lower() == target:
                matches.append(route)
                break
    return matches


def normalize_ip_objects(entries: Sequence[Any]) -> list[str]:
    """Normalize a route payload list of strings or objects into canonical strings."""

    values: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            values.append(canonicalize_ip_token(entry))
            continue
        if isinstance(entry, dict):
            for key in ("ip_or_subnet", "ip", "value"):
                value = entry.get(key)
                if isinstance(value, str):
                    values.append(canonicalize_ip_token(value))
                    break
            else:
                raise UnsupportedRouteShapeError(
                    f"Unsupported IP object in route: {json.dumps(entry, ensure_ascii=True, sort_keys=True)}"
                )
            continue
        raise UnsupportedRouteShapeError(f"Unsupported IP entry type: {type(entry)!r}")
    return sort_ip_tokens(values)


def infer_common_item_fields(entries: Sequence[Any]) -> dict[str, Any]:
    """Infer shared object fields so updates preserve object shape where possible."""

    dict_entries = [entry for entry in entries if isinstance(entry, dict)]
    if not dict_entries:
        return {}
    common_keys = set(dict_entries[0].keys())
    for entry in dict_entries[1:]:
        common_keys &= set(entry.keys())
    common_keys -= {"ip_or_subnet", "ip", "value"}
    result: dict[str, Any] = {}
    for key in common_keys:
        first_value = dict_entries[0].get(key)
        if all(entry.get(key) == first_value for entry in dict_entries[1:]):
            result[key] = copy.deepcopy(first_value)
    return result


def format_ip_version(example_value: Any, ip_token: str) -> str | None:
    """Format a version field in the same style as the existing payload."""

    if example_value is None:
        return None
    version = 6 if ":" in ip_token else 4
    example = str(example_value)
    mapping = {
        "IPv4": "IPv4",
        "IPv6": "IPv6",
        "IPV4": "IPV4",
        "IPV6": "IPV6",
        "v4": "v4",
        "v6": "v6",
        "V4": "V4",
        "V6": "V6",
        "4": "4",
        "6": "6",
    }
    if example not in mapping:
        return None
    if example.startswith(("IPv", "IPV")):
        prefix = example[:-1]
        return f"{prefix}{version}"
    if example.startswith(("v", "V")):
        return f"{example[0]}{version}"
    return str(version)


def build_ip_objects(desired: Sequence[str], existing_entries: Sequence[Any]) -> list[dict[str, Any]]:
    """Build object-style IP entries following the shape of the current payload."""

    extras = infer_common_item_fields(existing_entries)
    dict_entries = [entry for entry in existing_entries if isinstance(entry, dict)]
    version_key = None
    subnet_key = None
    for key in ("ip_version", "ipVersion"):
        if any(key in entry for entry in dict_entries):
            version_key = key
            break
    for key in ("ip_or_subnet", "ipOrSubnet"):
        if any(key in entry for entry in dict_entries):
            subnet_key = key
            break

    built: list[dict[str, Any]] = []
    for token in desired:
        item = copy.deepcopy(extras)
        item[subnet_key or "ip_or_subnet"] = token
        if version_key:
            example_version = None
            for entry in dict_entries:
                if version_key in entry:
                    example_version = entry[version_key]
                    break
            formatted = format_ip_version(example_version, token)
            if formatted is not None:
                item[version_key] = formatted
            else:
                item.pop(version_key, None)
        built.append(item)
    if not built and existing_entries:
        return []
    return built or [{subnet_key or "ip_or_subnet": token} for token in desired]


def get_nested(payload: dict[str, Any], path: str) -> Any:
    """Read a dotted path from a dictionary."""

    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        if part not in current:
            return None
        current = current[part]
    return current


def set_nested(payload: dict[str, Any], path: str, value: Any, *, create_missing: bool) -> None:
    """Write a dotted path into a dictionary."""

    current: dict[str, Any] = payload
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if next_value is None:
            if not create_missing:
                raise UnsupportedRouteShapeError(f"Path {path!r} does not exist in route payload")
            current[part] = {}
            next_value = current[part]
        if not isinstance(next_value, dict):
            raise UnsupportedRouteShapeError(f"Path {path!r} cannot be set because {part!r} is not a dict")
        current = next_value
    current[parts[-1]] = value


def normalize_mac(value: str) -> str:
    """Normalize a MAC address to lower-case colon-separated form."""

    raw = value.strip().lower().replace("-", ":")
    compact = raw.replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise ValueError(f"Invalid MAC address: {value!r}")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def direct_ip_entries(desired_ips: Sequence[str]) -> list[dict[str, Any]]:
    """Build the concrete ip_addresses shape used by UniFi traffic routes."""

    entries: list[dict[str, Any]] = []
    for token in desired_ips:
        entries.append(
            {
                "ip_or_subnet": token,
                "ip_version": "v6" if ":" in token else "v4",
                "ports": [],
                "port_ranges": [],
            }
        )
    return entries


class UniFiClient:
    """Stateful UniFi HTTP client for the local UniFi API."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.unifi")
        self.csrf_token: str | None = None
        self.logged_in = False
        self.login_path: str | None = None
        self.network_prefix: str | None = None
        self.site_context: SiteContext | None = None

        cookie_jar = http.cookiejar.CookieJar()
        context = make_ssl_context(verify=config.unifi_verify_tls, ca_file=config.unifi_ca_file)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(context=context),
        )

    @property
    def base_url(self) -> str:
        if not self.config.host:
            raise ConfigError("host is required for local mode")
        return f"https://{self.config.host}:{self.config.port}"

    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}{path}"

    def _update_csrf_token(self, headers: Any) -> None:
        token = None
        if headers is not None:
            token = headers.get("X-CSRF-Token") or headers.get("x-csrf-token")
        if token:
            self.csrf_token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        expected_statuses: Sequence[int] = (200,),
        require_json: bool = True,
        retry_on_auth: bool = True,
    ) -> Any:
        """Perform an API request through the selected UniFi transport."""

        body_bytes = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token

        url = self._build_url(path)
        attempts = max(1, self.config.retries)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
            try:
                with self.opener.open(request, timeout=self.config.request_timeout) as response:
                    self._update_csrf_token(response.headers)
                    raw = response.read()
                    text = raw.decode("utf-8", errors="replace")
                    if response.status not in expected_statuses:
                        raise RemoteRequestError(f"{method} {path} returned {response.status}: {text[:500]}")
                    if not text:
                        return None
                    if not require_json:
                        return text
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise RemoteRequestError(f"{method} {path} returned invalid JSON: {text[:500]}") from exc
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                    self._update_csrf_token(exc.headers)
                    if (
                        exc.code == 401
                        and retry_on_auth
                        and self.logged_in
                        and self.login_path
                        and path != self.login_path
                    ):
                        log_event(self.logger, logging.WARNING, "unifi_relogin", path=path)
                        self.login(force=True)
                        return self.request(
                            method,
                            path,
                            json_body=json_body,
                            expected_statuses=expected_statuses,
                            require_json=require_json,
                            retry_on_auth=False,
                        )
                    if exc.code in {429, 500, 502, 503, 504} and attempt < attempts:
                        log_event(
                            self.logger,
                            logging.WARNING,
                            "unifi_retry_http",
                            method=method,
                            path=path,
                            status=exc.code,
                            attempt=attempt,
                            retries=attempts,
                        )
                        sleep_with_backoff(attempt)
                        continue
                    raise RemoteRequestError(f"{method} {path} returned {exc.code}: {body[:700]}") from exc
                finally:
                    exc.close()
            except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
                last_error = exc
                if attempt < attempts:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "unifi_retry_network",
                        method=method,
                        path=path,
                        attempt=attempt,
                        retries=attempts,
                        error=exc,
                    )
                    sleep_with_backoff(attempt)
                    continue
                raise NetworkError(f"Network failure for {method} {path}: {exc}") from exc
        raise NetworkError(f"Network failure for {method} {path}: {last_error}")

    def login(self, force: bool = False) -> None:
        """Authenticate to UniFi."""

        if self.logged_in and not force:
            return

        candidates = [
            ("/api/auth/login", {"username": self.config.username, "password": self.config.password, "rememberMe": True}),
            ("/api/auth/login", {"username": self.config.username, "password": self.config.password}),
            ("/api/login", {"username": self.config.username, "password": self.config.password}),
        ]
        errors: list[str] = []
        for path, payload in candidates:
            try:
                response = self.request("POST", path, json_body=payload, expected_statuses=(200,), retry_on_auth=False)
                if isinstance(response, dict) and response.get("meta", {}).get("rc") == "error":
                    errors.append(f"{path}: {response}")
                    continue
                self.logged_in = True
                self.login_path = path
                log_event(self.logger, logging.INFO, "unifi_login_ok", path=path)
                return
            except StopLigaError as exc:
                errors.append(str(exc))
        raise AuthenticationError("Unable to authenticate to UniFi: " + " | ".join(errors))

    def discover_network_prefix(self) -> str:
        """Detect whether UniFi Network is exposed under /proxy/network or root."""

        if self.network_prefix is not None:
            return self.network_prefix
        for prefix in ("/proxy/network", ""):
            try:
                payload = self.request("GET", f"{prefix}/api/self/sites")
                if extract_records(payload):
                    self.network_prefix = prefix
                    log_event(self.logger, logging.INFO, "network_prefix_detected", prefix=prefix or "/")
                    return prefix
            except StopLigaError:
                continue
        raise DiscoveryError("Unable to detect UniFi Network API prefix")

    def get_network_sites(self) -> list[dict[str, Any]]:
        payload = self.request("GET", f"{self.discover_network_prefix()}/api/self/sites")
        records = extract_records(payload)
        if not records:
            raise DiscoveryError("UniFi did not return any sites")
        return records

    def get_official_sites(self) -> list[dict[str, Any]]:
        paths: list[str] = []
        prefix = self.discover_network_prefix()
        for candidate in (prefix, "/proxy/network", ""):
            for suffix in ("/integration/v1/sites", "/v1/sites"):
                path = f"{candidate}{suffix}"
                if path not in paths:
                    paths.append(path)
        for path in paths:
            try:
                payload = self.request("GET", path)
                records = extract_records(payload)
                if records:
                    return records
            except StopLigaError:
                continue
        return []

    def list_networks(self) -> list[dict[str, Any]]:
        """Return network definitions from the local controller."""

        prefix = self.discover_network_prefix()
        payload = self.request("GET", f"{prefix}/api/s/{self.config.site}/rest/networkconf")
        records = extract_records(payload)
        if not records:
            raise DiscoveryError("UniFi did not return any networks from rest/networkconf")
        return records

    def resolve_vpn_network(self, vpn_name: str) -> dict[str, Any]:
        """Resolve a VPN client network by exact name or ID."""

        target = vpn_name.strip().lower()
        networks = self.list_networks()
        matches = []
        for record in networks:
            if record.get("purpose") != "vpn-client":
                continue
            record_id = str(record.get("_id", "")).strip().lower()
            record_name = str(record.get("name", "")).strip().lower()
            if target in {record_id, record_name}:
                matches.append(record)
        if not matches:
            available = [record.get("name") for record in networks if record.get("purpose") == "vpn-client"]
            raise DiscoveryError(
                f"VPN client network {vpn_name!r} not found. Available VPNs: {', '.join(str(item) for item in available)}"
            )
        if len(matches) > 1:
            raise DiscoveryError(f"VPN client network {vpn_name!r} is ambiguous")
        return matches[0]

    def list_clients(self) -> list[dict[str, Any]]:
        """Return clients from the local controller."""

        prefix = self.discover_network_prefix()
        payload = self.request("GET", f"{prefix}/api/s/{self.config.site}/stat/sta")
        return extract_records(payload)

    def resolve_target_devices(self, targets: Sequence[str]) -> list[dict[str, Any]]:
        """Resolve a mixed list of client hostnames and MAC addresses."""

        clients = self.list_clients()
        by_alias: dict[str, list[dict[str, Any]]] = {}
        for client in clients:
            aliases = set()
            for key in ("hostname", "name", "display_name", "mac", "_id"):
                value = client.get(key)
                if isinstance(value, str) and value.strip():
                    aliases.add(value.strip().lower())
            for alias in aliases:
                by_alias.setdefault(alias, []).append(client)

        resolved_macs: list[str] = []
        for raw_target in targets:
            target = raw_target.strip()
            if not target:
                continue
            try:
                resolved_macs.append(normalize_mac(target))
                continue
            except ValueError:
                pass
            matches = by_alias.get(target.lower(), [])
            if not matches:
                raise DiscoveryError(f"Client target {target!r} was not found among current UniFi clients")
            if len(matches) > 1:
                raise DiscoveryError(f"Client target {target!r} is ambiguous; use MAC addresses instead")
            client_mac = matches[0].get("mac")
            if not isinstance(client_mac, str) or not client_mac:
                raise DiscoveryError(f"Client target {target!r} does not expose a MAC address")
            resolved_macs.append(normalize_mac(client_mac))

        unique = sorted(set(resolved_macs))
        return [{"client_mac": mac, "type": "CLIENT"} for mac in unique]

    def pick_default_vpn_network(self) -> dict[str, Any]:
        """Pick a deterministic VPN client network for bootstrap fallback."""

        candidates = [record for record in self.list_networks() if record.get("purpose") == "vpn-client"]
        if not candidates:
            raise DiscoveryError("No VPN client networks are available for automatic route creation")
        candidates.sort(key=lambda item: (str(item.get("name", "")).lower(), str(item.get("_id", "")).lower()))
        return candidates[0]

    def pick_default_target_device(self) -> dict[str, Any]:
        """Pick a deterministic client device for bootstrap fallback."""

        candidates: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for client in self.list_clients():
            mac = client.get("mac")
            if not isinstance(mac, str) or not mac.strip():
                continue
            normalized = normalize_mac(mac)
            label = ""
            for key in ("hostname", "name", "display_name"):
                value = client.get(key)
                if isinstance(value, str) and value.strip():
                    label = value.strip().lower()
                    break
            candidates.append(((label, normalized), {"client_mac": normalized, "type": "CLIENT"}))
        if not candidates:
            raise DiscoveryError("No UniFi clients with MAC addresses are available for automatic route creation")
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def resolve_site_context(self) -> SiteContext:
        """Resolve the configured site across different UniFi API shapes."""

        if self.site_context is not None:
            return self.site_context

        network_sites = self.get_network_sites()
        official_sites = self.get_official_sites()
        network_record = select_record(network_sites, self.config.site)
        official_record = select_record(official_sites, self.config.site)

        if network_record is None and official_record is not None:
            network_record = match_record(network_sites, site_aliases(official_record))
        if official_record is None and network_record is not None:
            official_record = match_record(official_sites, site_aliases(network_record))
        if network_record is None and official_record is None:
            available = [pick_site_internal_name(site) or site.get("id") or site.get("_id") for site in network_sites]
            raise DiscoveryError(
                f"Unable to resolve site {self.config.site!r}. Visible sites: {', '.join(str(item) for item in available)}"
            )

        internal_name = (
            pick_site_internal_name(network_record or {})
            or pick_site_internal_name(official_record or {})
            or self.config.site
        )
        site_id = pick_site_id(official_record or {}) or pick_site_id(network_record or {})
        self.site_context = SiteContext(
            internal_name=internal_name,
            site_id=site_id,
            network_record=network_record,
            official_record=official_record,
        )
        log_event(self.logger, logging.INFO, "site_resolved", internal_name=internal_name, site_id=site_id)
        return self.site_context


class LinkedTrafficMatchingListHelper:
    """Read, update and verify linked traffic matching lists."""

    def __init__(self, client: UniFiClient, site_context: SiteContext):
        self.client = client
        self.site_context = site_context

    def _candidate_paths(self, list_id: str) -> list[str]:
        if not self.site_context.site_id:
            return []
        prefix = self.client.discover_network_prefix()
        return list(
            dict.fromkeys(
                [
                    f"{prefix}/integration/v1/sites/{self.site_context.site_id}/traffic-matching-lists/{list_id}",
                    f"{prefix}/v1/sites/{self.site_context.site_id}/traffic-matching-lists/{list_id}",
                ]
            )
        )

    def get(self, list_id: str) -> tuple[str, dict[str, Any]]:
        for path in self._candidate_paths(list_id):
            try:
                payload = self.client.request("GET", path)
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    return path, payload["data"]
                if isinstance(payload, dict):
                    return path, payload
            except StopLigaError:
                continue
        raise DiscoveryError(f"Unable to read linked traffic matching list {list_id}")

    def build_update(self, list_id: str, desired_ips: Sequence[str]) -> tuple[str, dict[str, Any], list[str], list[str]]:
        endpoint, current = self.get(list_id)
        list_type = current.get("type")
        current_items = current.get("items")
        if list_type not in {"IPV4_ADDRESSES", "IPV6_ADDRESSES"}:
            raise UnsupportedRouteShapeError(f"Linked traffic matching list {list_id} has unsupported type {list_type!r}")
        if not isinstance(current_items, list):
            raise UnsupportedRouteShapeError(f"Linked traffic matching list {list_id} does not expose items[]")
        current_destinations = sort_ip_tokens(str(item) for item in current_items)
        changed_fields: list[str] = []
        if current_destinations != list(desired_ips):
            changed_fields.append("linked_list.items")
        payload = {
            "type": list_type,
            "name": current.get("name"),
            "items": list(desired_ips),
        }
        return endpoint, payload, current_destinations, changed_fields

    def verify(self, list_id: str, desired_ips: Sequence[str]) -> None:
        _, payload = self.get(list_id)
        items = payload.get("items")
        if not isinstance(items, list):
            raise RemoteRequestError(f"Linked list {list_id} no longer exposes items[] after update")
        current = sort_ip_tokens(str(item) for item in items)
        if current != list(desired_ips):
            raise RemoteRequestError(f"Linked list {list_id} was not updated to the expected destinations")


class BaseRouteBackend:
    """Base class for concrete UniFi route backends."""

    backend_name = "base"
    update_method = "PUT"

    def __init__(self, client: UniFiClient, site_context: SiteContext):
        self.client = client
        self.site_context = site_context
        self.linked_lists = LinkedTrafficMatchingListHelper(client, site_context)

    def list_routes(self) -> tuple[str, list[dict[str, Any]]]:
        raise NotImplementedError

    def find_route(self, target_name: str) -> tuple[str, dict[str, Any]]:
        endpoint, routes = self.list_routes()
        matches = find_matching_routes(routes, target_name)
        if not matches:
            raise RouteNotFoundError(f"Route {target_name!r} not found in backend {self.backend_name}")
        if len(matches) > 1:
            raise DuplicateRouteError(f"Route {target_name!r} matched multiple entries in backend {self.backend_name}")
        return endpoint, matches[0]

    def get_route(self, route_id_value: str) -> tuple[str, dict[str, Any]]:
        endpoint, routes = self.list_routes()
        matches = [route for route in routes if route_id(route) == route_id_value]
        if not matches:
            raise RouteNotFoundError(f"Route {route_id_value!r} disappeared from backend {self.backend_name}")
        return endpoint, matches[0]

    def route_update_path(self, endpoint: str, route_record: dict[str, Any]) -> str:
        return f"{endpoint}/{route_id(route_record)}"

    def create_route(self, payload: dict[str, Any]) -> None:
        endpoint, _ = self.list_routes()
        self.client.request("POST", endpoint, json_body=payload, expected_statuses=(200, 201))

    def _detect_linked_list_id(self, route_record: dict[str, Any]) -> str | None:
        for key in (
            "trafficMatchingListId",
            "traffic_matching_list_id",
            "destinationTrafficMatchingListId",
            "destination_traffic_matching_list_id",
            "matchingListId",
        ):
            value = route_record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        destination = route_record.get("destination")
        if isinstance(destination, dict):
            for key in (
                "trafficMatchingListId",
                "traffic_matching_list_id",
                "destinationTrafficMatchingListId",
                "destination_traffic_matching_list_id",
                "matchingListId",
            ):
                value = destination.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _resolve_destination_path(self, route_record: dict[str, Any], *, allow_missing: bool) -> str:
        override = self.client.config.destination_field
        if override != "auto":
            if override == "linked_list.items":
                if not self._detect_linked_list_id(route_record):
                    raise UnsupportedRouteShapeError("destination_field=linked_list.items but route has no linked list ID")
                return override
            if not allow_missing and get_nested(route_record, override) is None:
                raise UnsupportedRouteShapeError(f"Configured destination field {override!r} is missing in route payload")
            return override

        for path in ("ip_addresses", "destinations", "destination.ip_addresses", "destination.ips", "destination.items"):
            if get_nested(route_record, path) is not None:
                return path
        if self._detect_linked_list_id(route_record):
            return "linked_list.items"
        raise UnsupportedRouteShapeError(
            f"Unable to identify an updatable destination field for route {route_label(route_record)!r}. "
            f"Available top-level fields: {', '.join(sorted(route_record.keys()))}"
        )

    def _build_destination_value(self, path: str, existing_entries: Sequence[Any], desired_ips: Sequence[str]) -> Any:
        if path.endswith("ip_addresses") or any(isinstance(item, dict) for item in existing_entries):
            return build_ip_objects(desired_ips, existing_entries)
        return list(desired_ips)

    def _build_route_payload_for_destinations(
        self,
        route_record: dict[str, Any],
        desired_ips: Sequence[str],
        *,
        allow_missing: bool,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        payload = copy.deepcopy(route_record)
        path = self._resolve_destination_path(payload, allow_missing=allow_missing)
        if path == "linked_list.items":
            return payload, [], []

        existing_entries = get_nested(payload, path)
        if existing_entries is None:
            existing_entries = []
        if not isinstance(existing_entries, list):
            raise UnsupportedRouteShapeError(f"Destination field {path!r} is not a list")

        current_destinations = normalize_ip_objects(existing_entries)
        desired_value = self._build_destination_value(path, existing_entries, desired_ips)
        set_nested(payload, path, desired_value, create_missing=allow_missing)
        if path == "ip_addresses" and "matching_target" in payload and not payload.get("matching_target"):
            payload["matching_target"] = "IP"
        changed_fields = [path] if current_destinations != list(desired_ips) else []
        return payload, current_destinations, changed_fields

    def build_plan(self, endpoint: str, route_record: dict[str, Any], desired_ips: Sequence[str], desired_enabled: bool) -> UpdatePlan:
        route_payload, current_destinations, route_changed_fields = self._build_route_payload_for_destinations(
            route_record,
            desired_ips,
            allow_missing=False,
        )
        linked_list_id = self._detect_linked_list_id(route_record)
        linked_list_endpoint = None
        linked_list_payload = None
        linked_list_current_destinations: list[str] = []
        linked_list_changed_fields: list[str] = []

        if self._resolve_destination_path(route_record, allow_missing=False) == "linked_list.items":
            if not linked_list_id:
                raise UnsupportedRouteShapeError("Route indicates linked_list.items but no linked list ID was found")
            (
                linked_list_endpoint,
                linked_list_payload,
                linked_list_current_destinations,
                linked_list_changed_fields,
            ) = self.linked_lists.build_update(linked_list_id, desired_ips)
            current_destinations = linked_list_current_destinations
            route_changed_fields = []

        current_enabled = route_record.get("enabled") if isinstance(route_record.get("enabled"), bool) else None
        if current_enabled != desired_enabled:
            route_payload["enabled"] = desired_enabled
            route_changed_fields.append("enabled")
        if not route_changed_fields:
            route_payload = None

        return UpdatePlan(
            backend_name=self.backend_name,
            route_id=route_id(route_record),
            route_label=route_label(route_record),
            route_endpoint=self.route_update_path(endpoint, route_record),
            route_method=self.update_method,
            current_enabled=current_enabled,
            desired_enabled=desired_enabled,
            current_destinations=current_destinations,
            desired_destinations=list(desired_ips),
            route_payload=route_payload,
            route_changed_fields=route_changed_fields,
            linked_list_id=linked_list_id,
            linked_list_endpoint=linked_list_endpoint,
            linked_list_payload=linked_list_payload,
            linked_list_changed_fields=linked_list_changed_fields,
            linked_list_current_destinations=linked_list_current_destinations,
            raw_route=copy.deepcopy(route_record),
        )

    def verify(self, route_id_value: str, desired_ips: Sequence[str], desired_enabled: bool) -> None:
        _, route_record = self.get_route(route_id_value)
        current_enabled = route_record.get("enabled")
        if current_enabled is not None and current_enabled != desired_enabled:
            raise RemoteRequestError(f"Route {route_label(route_record)!r} did not keep enabled={desired_enabled}")

        if self._resolve_destination_path(route_record, allow_missing=False) == "linked_list.items":
            linked_list_id = self._detect_linked_list_id(route_record)
            if not linked_list_id:
                raise RemoteRequestError("Linked list route lost its linked list identifier after update")
            self.linked_lists.verify(linked_list_id, desired_ips)
            return

        _, current_destinations, _ = self._build_route_payload_for_destinations(route_record, desired_ips, allow_missing=False)
        if current_destinations != list(desired_ips):
            raise RemoteRequestError(f"Route {route_label(route_record)!r} did not keep the expected destination list")


class V2TrafficRoutesBackend(BaseRouteBackend):
    """Modern trafficroutes backend."""

    backend_name = "v2-trafficroutes"

    def list_routes(self) -> tuple[str, list[dict[str, Any]]]:
        prefix = self.client.discover_network_prefix()
        endpoint = f"{prefix}/v2/api/site/{self.site_context.internal_name}/trafficroutes"
        payload = self.client.request("GET", endpoint)
        if not isinstance(payload, (dict, list)):
            raise DiscoveryError(f"Endpoint {endpoint} returned an unsupported shape")
        return endpoint, extract_records(payload)


class LegacyTrafficRouteRestBackend(BaseRouteBackend):
    """Legacy trafficroute REST backend."""

    backend_name = "legacy-rest-trafficroute"

    def list_routes(self) -> tuple[str, list[dict[str, Any]]]:
        prefix = self.client.discover_network_prefix()
        endpoint = f"{prefix}/api/s/{self.site_context.internal_name}/rest/trafficroute"
        payload = self.client.request("GET", endpoint)
        if not isinstance(payload, (dict, list)):
            raise DiscoveryError(f"Endpoint {endpoint} returned an unsupported shape")
        return endpoint, extract_records(payload)


def available_backends(client: UniFiClient, site_context: SiteContext) -> list[BaseRouteBackend]:
    return [V2TrafficRoutesBackend(client, site_context), LegacyTrafficRouteRestBackend(client, site_context)]


def choose_existing_route_backend(
    client: UniFiClient,
    site_context: SiteContext,
    route_name_value: str,
) -> tuple[BaseRouteBackend, str, dict[str, Any]]:
    """Find the backend containing the requested route."""

    errors: list[str] = []
    for backend in available_backends(client, site_context):
        try:
            endpoint, route_record = backend.find_route(route_name_value)
            log_event(
                logging.getLogger("stopliga.route"),
                logging.INFO,
                "route_found",
                backend=backend.backend_name,
                endpoint=endpoint,
                route=route_name_value,
            )
            return backend, endpoint, route_record
        except DuplicateRouteError:
            raise
        except StopLigaError as exc:
            errors.append(f"{backend.backend_name}: {exc}")
    raise RouteNotFoundError(f"Unable to find route {route_name_value!r}. " + " | ".join(errors))


def choose_create_backend(client: UniFiClient, site_context: SiteContext) -> BaseRouteBackend:
    """Find a backend whose collection endpoint is reachable for bootstrap."""

    errors: list[str] = []
    for backend in available_backends(client, site_context):
        try:
            backend.list_routes()
            return backend
        except StopLigaError as exc:
            errors.append(f"{backend.backend_name}: {exc}")
    raise DiscoveryError("Unable to find a writable route backend for bootstrap. " + " | ".join(errors))


def build_direct_bootstrap_payload(
    *,
    route_name_value: str,
    desired_ips: Sequence[str],
    desired_enabled: bool,
    vpn_network_id: str,
    target_devices: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Build a direct traffic route payload using the native shape observed on UniFi."""

    return {
        "description": route_name_value,
        "domains": [],
        "enabled": desired_enabled,
        "ip_addresses": direct_ip_entries(desired_ips),
        "ip_ranges": [],
        "kill_switch_enabled": False,
        "matching_target": "IP",
        "network_id": vpn_network_id,
        "next_hop": "",
        "regions": [],
        "target_devices": list(target_devices),
    }


def apply_plan(client: UniFiClient, backend: BaseRouteBackend, plan: UpdatePlan) -> None:
    """Apply a route update plan and verify the resulting state."""

    logger = logging.getLogger("stopliga.apply")
    if plan.linked_list_payload and plan.linked_list_endpoint:
        log_event(logger, logging.INFO, "linked_list_updating", linked_list_id=plan.linked_list_id)
        client.request("PUT", plan.linked_list_endpoint, json_body=plan.linked_list_payload, expected_statuses=(200, 204))
    if plan.route_payload:
        log_event(
            logger,
            logging.INFO,
            "route_updating",
            route_id=plan.route_id,
            backend=plan.backend_name,
            method=plan.route_method,
        )
        client.request(plan.route_method, plan.route_endpoint, json_body=plan.route_payload, expected_statuses=(200, 201, 204))
    backend.verify(plan.route_id, plan.desired_destinations, plan.desired_enabled)


def summarize_plan(plan: UpdatePlan, feed_snapshot: FeedSnapshot) -> str:
    """Return a concise log summary for a route plan."""

    parts = [
        f"backend={plan.backend_name}",
        f"route={plan.route_label}",
        f"route_id={plan.route_id}",
        f"is_blocked={feed_snapshot.is_blocked}",
        f"enabled_current={plan.current_enabled}",
        f"enabled_desired={plan.desired_enabled}",
        f"feed_destinations={len(plan.desired_destinations)}",
        f"current_destinations={len(plan.current_destinations)}",
    ]
    if plan.route_changed_fields:
        parts.append("route_changes=" + ",".join(plan.route_changed_fields))
    if plan.linked_list_changed_fields:
        parts.append("linked_list_changes=" + ",".join(plan.linked_list_changed_fields))
    if not plan.has_changes:
        parts.append("status=noop")
    return " | ".join(parts)


def log_unsupported_shape(logger: logging.Logger, route_record: dict[str, Any]) -> None:
    """Emit a truncated route payload when the shape is not supported."""

    log_event(logger, logging.ERROR, "unsupported_route_shape", payload=shorten_json(route_record))
