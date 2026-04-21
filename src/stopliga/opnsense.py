"""OPNsense API client and alias/rule synchronization logic."""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

from .errors import AuthenticationError, ConfigError, DiscoveryError, NetworkError, RemoteRequestError
from .logging_utils import log_event
from .models import Config, FeedSnapshot, SyncResult
from .utils import make_ssl_context, read_limited, sleep_with_backoff, sort_ip_tokens


SAFE_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def sanitize_alias_name(name: str) -> str:
    """Convert a route name to a valid OPNsense alias name."""

    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    if not sanitized:
        return "StopLiga"
    if sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized[:32]


def _is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def parse_alias_content(alias_record: dict[str, Any]) -> list[str]:
    """Normalize IPs from an alias response into a sorted canonical list."""

    content = alias_record.get("content", {})
    raw: list[str] = []
    if isinstance(content, str):
        raw = [line.strip() for line in content.splitlines() if line.strip()]
    elif isinstance(content, dict):
        if any(isinstance(item, dict) for item in content.values()):
            for key, item in content.items():
                if not isinstance(item, dict):
                    if isinstance(item, str) and item.strip():
                        raw.append(item.strip())
                    continue
                if not _is_truthy_flag(item.get("selected")):
                    continue
                value = item.get("value", key)
                if isinstance(value, str) and value.strip():
                    raw.append(value.strip())
        else:
            raw = [value.strip() for value in content.values() if isinstance(value, str) and value.strip()]
    elif isinstance(content, list):
        raw = [str(item).strip() for item in content if str(item).strip()]
    try:
        return sort_ip_tokens(raw)
    except Exception:
        return sorted(set(raw))


class OPNsenseClient:
    """Stateless OPNsense HTTP client using Basic Auth."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.opnsense")
        context = make_ssl_context(
            verify=config.opnsense_verify_tls,
            ca_file=config.opnsense_ca_file,
        )
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    @property
    def base_url(self) -> str:
        if not self.config.opnsense_host:
            raise ConfigError("OPNSENSE_HOST is required")
        host = self.config.opnsense_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"https://{host}"

    def _auth_header(self) -> str:
        import base64

        key = self.config.opnsense_api_key or ""
        secret = self.config.opnsense_api_secret or ""
        return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        expected_statuses: Sequence[int] = (200,),
    ) -> Any:
        method_name = method.upper()
        url = f"{self.base_url}/api{path}"
        body_bytes: bytes | None = None
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Authorization": self._auth_header(),
        }

        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method_name == "POST":
            body_bytes = b""

        should_retry = method_name in SAFE_RETRY_METHODS
        attempts = max(1, self.config.retries) if should_retry else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=body_bytes, headers=headers, method=method_name)
            try:
                with self.opener.open(request, timeout=self.config.request_timeout) as response:
                    try:
                        raw = read_limited(
                            response,
                            max_bytes=self.config.max_response_bytes,
                            content_length=response.headers.get("Content-Length"),
                        )
                    except ValueError as exc:
                        raise RemoteRequestError(
                            f"{method_name} {path} returned an oversized response: {exc}"
                        ) from exc
                    text = raw.decode("utf-8", errors="replace")
                    if response.status not in expected_statuses:
                        raise RemoteRequestError(
                            f"{method_name} {path} returned {response.status}: {text[:500]}"
                        )
                    if not text:
                        return None
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise RemoteRequestError(
                            f"{method_name} {path} returned invalid JSON: {text[:500]}"
                        ) from exc
            except urllib.error.HTTPError as exc:
                try:
                    try:
                        err_raw = read_limited(
                            exc,
                            max_bytes=min(self.config.max_response_bytes, 256 * 1024),
                            content_length=exc.headers.get("Content-Length") if exc.headers else None,
                        )
                    except ValueError:
                        err_text = "<oversized error response>"
                    else:
                        err_text = err_raw.decode("utf-8", errors="replace")
                    if exc.code in {401, 403}:
                        raise AuthenticationError(
                            f"OPNsense auth failed for {method_name} {path}: "
                            "check OPNSENSE_API_KEY / OPNSENSE_API_SECRET"
                        ) from exc
                    if should_retry and exc.code in {429, 500, 502, 503, 504} and attempt < attempts:
                        log_event(
                            self.logger,
                            logging.WARNING,
                            "opnsense_retry_http",
                            method=method_name,
                            path=path,
                            status=exc.code,
                            attempt=attempt,
                        )
                        sleep_with_backoff(attempt)
                        continue
                    raise RemoteRequestError(
                        f"{method_name} {path} returned {exc.code}: {err_text[:700]}"
                    ) from exc
                finally:
                    exc.close()
            except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
                last_error = exc
                if attempt < attempts:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "opnsense_retry_network",
                        method=method_name,
                        path=path,
                        attempt=attempt,
                        error=exc,
                    )
                    sleep_with_backoff(attempt)
                    continue
                raise NetworkError(f"Network failure for {method_name} {path}: {exc}") from exc
        raise NetworkError(f"Network failure for {method_name} {path}: {last_error}")

    def authenticate(self) -> None:
        if not self.config.opnsense_api_key or not self.config.opnsense_api_secret:
            raise AuthenticationError("OPNSENSE_API_KEY and OPNSENSE_API_SECRET are required")
        self.request("GET", "/firewall/alias/get")

    def search_alias(self, name: str) -> dict[str, Any] | None:
        """Return the alias row whose name exactly matches."""

        search_phrase = urllib.parse.quote(name, safe="")
        payload = self.request("GET", f"/firewall/alias/searchItem?searchPhrase={search_phrase}")
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        for row in rows:
            if isinstance(row, dict) and row.get("name", "").strip().lower() == name.strip().lower():
                return row
        return None

    def get_alias_item(self, uuid: str) -> dict[str, Any]:
        """Return the alias record, unwrapping the alias envelope if present."""

        payload = self.request("GET", f"/firewall/alias/getItem/{uuid}")
        if isinstance(payload, dict) and "alias" in payload and isinstance(payload["alias"], dict):
            return payload["alias"]
        return payload if isinstance(payload, dict) else {}

    def create_alias(self, name: str, content: list[str]) -> str:
        payload = {
            "alias": {
                "name": name,
                "type": "host",
                "description": f"StopLiga managed: {name}",
                "content": "\n".join(content),
                "enabled": "1",
            }
        }
        result = self.request("POST", "/firewall/alias/addItem", json_body=payload)
        uuid = result.get("uuid") if isinstance(result, dict) else None
        if not uuid:
            raise DiscoveryError(f"OPNsense did not return a UUID after creating alias {name!r}")
        return str(uuid)

    def update_alias_content(self, uuid: str, name: str, content: list[str]) -> None:
        payload = {
            "alias": {
                "name": name,
                "type": "host",
                "description": f"StopLiga managed: {name}",
                "content": "\n".join(content),
                "enabled": "1",
            }
        }
        self.request("POST", f"/firewall/alias/setItem/{uuid}", json_body=payload)

    def reconfigure_alias(self) -> None:
        self.request("POST", "/firewall/alias/reconfigure")

    def search_rule(self, description: str) -> dict[str, Any] | None:
        """Return the first rule whose description exactly matches."""

        search_phrase = urllib.parse.quote(description, safe="")
        payload = self.request("GET", f"/firewall/filter/searchRule?searchPhrase={search_phrase}")
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        for row in rows:
            if (
                isinstance(row, dict)
                and row.get("description", "").strip().lower() == description.strip().lower()
            ):
                return row
        return None

    def toggle_rule(self, uuid: str, enabled: bool) -> None:
        self.request("POST", f"/firewall/filter/toggleRule/{uuid}/{1 if enabled else 0}")

    def apply_filter(self) -> None:
        self.request("POST", "/firewall/filter/apply")


def sync_opnsense(config: Config, feed_snapshot: FeedSnapshot) -> SyncResult:
    """Synchronize OPNsense alias IPs and firewall rule state."""

    client = OPNsenseClient(config)
    client.authenticate()

    alias_name = config.opnsense_alias_name or sanitize_alias_name(config.route_name)
    rule_description = config.route_name
    desired_ips = list(feed_snapshot.destinations)
    desired_enabled = feed_snapshot.desired_enabled
    logger = logging.getLogger("stopliga.opnsense")

    existing_alias = client.search_alias(alias_name)
    alias_created = False
    current_ips: list[str] = []

    if existing_alias is None:
        log_event(
            logger,
            logging.INFO,
            "opnsense_alias_create",
            alias=alias_name,
            dry_run=config.dry_run,
        )
        if not config.dry_run:
            client.create_alias(alias_name, desired_ips)
            client.reconfigure_alias()
        alias_created = True
    else:
        alias_uuid = str(existing_alias.get("uuid", "")).strip()
        if not alias_uuid:
            raise DiscoveryError(f"Alias {alias_name!r} was found but did not include a UUID")
        alias_record = client.get_alias_item(alias_uuid)
        current_ips = parse_alias_content(alias_record)
        ips_changed = current_ips != desired_ips
        log_event(
            logger,
            logging.INFO,
            "opnsense_alias_check",
            alias=alias_name,
            alias_uuid=alias_uuid,
            current_destinations=len(current_ips),
            desired_destinations=len(desired_ips),
            ips_changed=ips_changed,
        )
        if ips_changed and not config.dry_run:
            client.update_alias_content(alias_uuid, alias_name, desired_ips)
            client.reconfigure_alias()

    rule_record = client.search_rule(rule_description)
    if rule_record is None:
        raise DiscoveryError(
            f"Firewall rule {rule_description!r} not found in OPNsense. "
            "Create a rule with that exact description and restart StopLiga."
        )
    rule_uuid = str(rule_record.get("uuid", "")).strip()
    if not rule_uuid:
        raise DiscoveryError(f"Firewall rule {rule_description!r} was found but did not include a UUID")

    current_enabled = _is_truthy_flag(rule_record.get("enabled"))
    rule_changed = current_enabled != desired_enabled

    log_event(
        logger,
        logging.INFO,
        "opnsense_rule_check",
        rule=rule_description,
        rule_uuid=rule_uuid,
        current_enabled=current_enabled,
        desired_enabled=desired_enabled,
        rule_changed=rule_changed,
    )

    if rule_changed and not config.dry_run:
        client.toggle_rule(rule_uuid, desired_enabled)
        client.apply_filter()

    added = len([ip for ip in desired_ips if ip not in current_ips])
    removed = len([ip for ip in current_ips if ip not in desired_ips])
    changed = alias_created or current_ips != desired_ips or rule_changed

    return SyncResult(
        mode="opnsense",
        route_name=config.route_name,
        route_id=rule_uuid,
        backend_name="opnsense-alias+rule",
        changed=changed,
        created=alias_created,
        dry_run=config.dry_run,
        desired_enabled=desired_enabled,
        current_enabled=current_enabled,
        desired_destinations=len(desired_ips),
        current_destinations=len(current_ips),
        invalid_entries=feed_snapshot.invalid_count,
        feed_hash=feed_snapshot.feed_hash,
        destinations_hash=feed_snapshot.destinations_hash,
        summary=(
            f"alias={alias_name} rule={rule_description!r} "
            f"enabled={desired_enabled} ips={len(desired_ips)} added={added} removed={removed}"
        ),
        is_blocked=feed_snapshot.is_blocked,
        added_destinations=added,
        removed_destinations=removed,
        bootstrap_source="alias-created" if alias_created else None,
    )
