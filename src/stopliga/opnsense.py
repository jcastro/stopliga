"""OPNsense API client and alias/rule synchronization logic."""

from __future__ import annotations

from collections.abc import Callable

import base64
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

from .errors import (
    AuthenticationError,
    ConfigError,
    DiscoveryError,
    DuplicateRouteError,
    NetworkError,
    RemoteRequestError,
    PartialUpdateError,
    StopLigaError,
)
from .logging_utils import log_event
from .models import Config, FeedSnapshot, SyncResult
from .utils import compact_json_bytes, make_ssl_context, read_limited, sleep_with_backoff, sort_ip_tokens


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
        self.before_write: Callable[[], None] | None = None
        self.logger = logging.getLogger("stopliga.opnsense")
        context = make_ssl_context(
            verify=config.opnsense_verify_tls,
            ca_file=config.opnsense_ca_file,
        )
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
        self.auth_header = self._build_auth_header(config)

    @property
    def base_url(self) -> str:
        if not self.config.opnsense_host:
            raise ConfigError("OPNSENSE_HOST is required")
        host = self.config.opnsense_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"https://{host}"

    @staticmethod
    def _build_auth_header(config: Config) -> str:
        key = config.opnsense_api_key or ""
        secret = config.opnsense_api_secret or ""
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
        if method_name not in SAFE_RETRY_METHODS and self.before_write is not None:
            self.before_write()
        url = f"{self.base_url}/api{path}"
        body_bytes: bytes | None = None
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Authorization": self.auth_header,
        }

        if json_body is not None:
            body_bytes = compact_json_bytes(json_body)
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
                        raise RemoteRequestError(f"{method_name} {path} returned an oversized response: {exc}") from exc
                    text = raw.decode("utf-8", errors="replace")
                    if response.status not in expected_statuses:
                        raise RemoteRequestError(f"{method_name} {path} returned {response.status}: {text[:500]}")
                    if not text:
                        return None
                    try:
                        payload = json.loads(text)
                        if isinstance(payload, dict):
                            for key in ("result", "status"):
                                value = payload.get(key)
                                if isinstance(value, str) and value.lower() in {"failed", "error", "failure"}:
                                    raise RemoteRequestError(f"OPNsense {method_name} {path} reported a failure")
                            if payload.get("validations"):
                                raise RemoteRequestError(f"OPNsense {method_name} {path} rejected validation")
                        return payload
                    except json.JSONDecodeError as exc:
                        raise RemoteRequestError(f"{method_name} {path} returned invalid JSON: {text[:500]}") from exc
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
                    raise RemoteRequestError(f"{method_name} {path} returned {exc.code}: {err_text[:700]}") from exc
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
        payload = self.request("GET", f"/firewall/alias/searchItem?current=1&rowCount=-1&searchPhrase={search_phrase}")
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise RemoteRequestError("OPNsense search returned invalid rows")
        total = payload.get("total") if isinstance(payload, dict) else None
        if total is not None:
            try:
                complete = int(total) == len(rows)
            except (ValueError, TypeError):
                complete = False
            if not complete:
                raise RemoteRequestError("OPNsense search returned an incomplete inventory")
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("name", "")).strip().lower() == name.strip().lower()
        ]
        if len(matches) > 1:
            raise DuplicateRouteError("Multiple OPNsense objects matched the managed name")
        return matches[0] if matches else None

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

    def delete_alias(self, uuid: str) -> None:
        self.request("POST", f"/firewall/alias/delItem/{uuid}")

    def reconfigure_alias(self) -> None:
        self.request("POST", "/firewall/alias/reconfigure")

    def search_rule(self, description: str) -> dict[str, Any] | None:
        """Return the unique rule whose description exactly matches."""

        search_phrase = urllib.parse.quote(description, safe="")
        payload = self.request("GET", f"/firewall/filter/searchRule?current=1&rowCount=-1&searchPhrase={search_phrase}")
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise RemoteRequestError("OPNsense search returned invalid rows")
        total = payload.get("total") if isinstance(payload, dict) else None
        if total is not None:
            try:
                complete = int(total) == len(rows)
            except (ValueError, TypeError):
                complete = False
            if not complete:
                raise RemoteRequestError("OPNsense search returned an incomplete inventory")
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("description", "")).strip().lower() == description.strip().lower()
        ]
        if len(matches) > 1:
            raise DuplicateRouteError("Multiple OPNsense objects matched the managed description")
        return matches[0] if matches else None

    def toggle_rule(self, uuid: str, enabled: bool) -> None:
        self.request("POST", f"/firewall/filter/toggleRule/{uuid}/{1 if enabled else 0}")

    def apply_filter(self) -> None:
        self.request("POST", "/firewall/filter/apply")


def sync_opnsense(config: Config, feed_snapshot: FeedSnapshot, *, client: OPNsenseClient | None = None) -> SyncResult:
    """Reconcile a unique alias/rule, verify writes and restore confirmed partial changes."""
    client = client or OPNsenseClient(config)
    client.authenticate()
    alias_name = config.opnsense_alias_name or sanitize_alias_name(config.route_name)
    desired_ips = list(feed_snapshot.destinations)
    desired_enabled = bool(feed_snapshot.desired_enabled and desired_ips)
    existing_alias = client.search_alias(alias_name)
    rule = client.search_rule(config.route_name)
    if rule is None and (desired_enabled or desired_ips):
        raise DiscoveryError(
            f"Firewall rule {config.route_name!r} not found in OPNsense. "
            "Create it in Firewall > Rules [new] (Firewall > Automation > Filter on older versions) before syncing"
        )
    rule_id = str(rule.get("uuid", "")).strip() if rule else None
    if rule is not None and not rule_id:
        raise DiscoveryError("OPNsense rule does not expose a UUID")
    current_enabled = _is_truthy_flag(rule.get("enabled")) if rule else None
    alias_id = str(existing_alias.get("uuid", "")).strip() if existing_alias else None
    if existing_alias is not None and not alias_id:
        raise DiscoveryError("OPNsense alias does not expose a UUID")
    current_ips = parse_alias_content(client.get_alias_item(alias_id)) if alias_id else []
    preserve = not desired_enabled and not desired_ips
    alias_changed = not preserve and current_ips != desired_ips
    alias_created = existing_alias is None and bool(desired_ips)
    rule_changed = rule is not None and current_enabled != desired_enabled
    completed: list[str] = []
    stage = "prepare"
    if not config.dry_run:
        try:
            # Close the rule before editing its alias when disabling.
            if rule_changed and not desired_enabled and rule_id:
                stage = "rule-disable"
                client.toggle_rule(rule_id, False)
                completed.append("rule")
                client.apply_filter()
            if alias_created:
                stage = "alias-create"
                alias_id = client.create_alias(alias_name, desired_ips)
                completed.append("alias-create")
                client.reconfigure_alias()
            elif alias_changed and alias_id:
                stage = "alias-update"
                client.update_alias_content(alias_id, alias_name, desired_ips)
                completed.append("alias-update")
                client.reconfigure_alias()
            stage = "verify-alias"
            if alias_changed or alias_created:
                if not alias_id or parse_alias_content(client.get_alias_item(alias_id)) != desired_ips:
                    raise RemoteRequestError("OPNsense alias did not keep its expected destinations")
            if rule_changed and desired_enabled and rule_id:
                stage = "rule-enable"
                client.toggle_rule(rule_id, True)
                completed.append("rule")
                client.apply_filter()
            stage = "verify-rule"
            if rule_changed:
                refreshed = client.search_rule(config.route_name)
                if refreshed is None or _is_truthy_flag(refreshed.get("enabled")) != desired_enabled:
                    raise RemoteRequestError("OPNsense rule did not keep its expected enabled state")
        except StopLigaError as exc:
            if isinstance(exc, NetworkError):
                raise PartialUpdateError(
                    stage,
                    tuple(completed + ["uncertain-write"]),
                    "OPNsense write outcome is unknown; reconcile before retrying",
                ) from exc
            rollback_error = None
            try:
                if "rule" in completed and rule_id:
                    client.toggle_rule(rule_id, False)
                    client.apply_filter()
                if "alias-create" in completed and alias_id:
                    client.delete_alias(alias_id)
                    client.reconfigure_alias()
                elif "alias-update" in completed and alias_id:
                    client.update_alias_content(alias_id, alias_name, current_ips)
                    client.reconfigure_alias()
                if "rule" in completed and rule_id and current_enabled:
                    client.toggle_rule(rule_id, True)
                    client.apply_filter()
                if "rule" in completed:
                    restored = client.search_rule(config.route_name)
                    if restored is None or _is_truthy_flag(restored.get("enabled")) != current_enabled:
                        raise RemoteRequestError("OPNsense rollback could not verify the original rule")
                if "alias-create" in completed and client.search_alias(alias_name) is not None:
                    raise RemoteRequestError("OPNsense rollback left a newly created alias")
                if "alias-update" in completed and alias_id:
                    if parse_alias_content(client.get_alias_item(alias_id)) != current_ips:
                        raise RemoteRequestError("OPNsense rollback could not verify the original alias")
            except StopLigaError as rollback_exc:
                rollback_error = str(rollback_exc)
            raise PartialUpdateError(
                stage,
                tuple(completed),
                str(exc),
                rollback_attempted=bool(completed),
                rollback_completed=bool(completed) and rollback_error is None,
                rollback_error=rollback_error,
            ) from exc
    added = len(set(desired_ips) - set(current_ips)) if not preserve else 0
    removed = len(set(current_ips) - set(desired_ips)) if not preserve else 0
    return SyncResult(
        mode="opnsense",
        route_name=config.route_name,
        route_id=rule_id,
        backend_name="opnsense-alias+rule",
        changed=alias_created or alias_changed or rule_changed,
        created=alias_created,
        dry_run=config.dry_run,
        desired_enabled=desired_enabled,
        current_enabled=current_enabled,
        desired_destinations=len(desired_ips),
        current_destinations=len(current_ips),
        invalid_entries=feed_snapshot.invalid_count,
        feed_hash=feed_snapshot.feed_hash,
        destinations_hash=feed_snapshot.destinations_hash,
        summary=f"alias={alias_name} enabled={desired_enabled} ips={len(desired_ips)}",
        is_blocked=feed_snapshot.is_blocked,
        added_destinations=added,
        removed_destinations=removed,
        bootstrap_source="alias-created" if alias_created else None,
    )
