from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config, FeedSnapshot  # noqa: E402
from stopliga.errors import DiscoveryError, RemoteRequestError, UnsupportedRouteShapeError  # noqa: E402
from stopliga.routers.omada import OmadaRouterDriver, OmadaSite  # noqa: E402
from stopliga.service import StopLigaService  # noqa: E402


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass
class FakeOmadaState:
    status_payload: dict[str, Any]
    ip_lines: list[str]
    access_token: str = "test-token"
    expected_client_id: str = "client-id"
    expected_client_secret: str = "client-secret"
    expected_omadac_id: str = "omadac-id"
    sites: list[dict[str, Any]] = field(default_factory=lambda: [{"siteId": "site-1", "name": "Default"}])
    lan_networks: list[dict[str, Any]] = field(default_factory=lambda: [{"id": "lan-1", "name": "LAN"}])
    wans: list[dict[str, Any]] = field(default_factory=list)
    wireguards: list[dict[str, Any]] = field(default_factory=list)
    site_to_site_vpns: list[dict[str, Any]] = field(default_factory=list)
    client_to_site_vpns: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    policy_routes: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[str] = field(default_factory=list)
    request_bodies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    api_error_once: dict[str, tuple[int, str]] = field(default_factory=dict)
    auth_call_count: int = 0
    group_counter: int = 0
    route_counter: int = 0

    def __post_init__(self) -> None:
        if "data" in self.status_payload:
            return
        blocked = bool(
            self.status_payload.get("isBlocked")
            or self.status_payload.get("blocked")
            or self.status_payload.get("state") in {"blocked", "active", "enabled"}
        )
        self.status_payload = {
            "lastUpdate": "2026-04-23 09:16:40",
            "data": [
                {
                    "ip": ip,
                    "description": "Cloudflare",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": blocked}],
                }
                for ip in self.ip_lines
            ],
        }


class FakeOmadaHandler(BaseHTTPRequestHandler):
    server_version = "FakeOmada/1.0"

    @property
    def state(self) -> FakeOmadaState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        self.state.request_bodies.append((self.path, clone(payload)))
        return payload

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, payload: str) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"AccessToken={self.state.access_token}"

    def _record_request(self) -> None:
        self.state.request_log.append(f"{self.command} {self.path}")

    def _maybe_send_injected_error(self, path: str) -> bool:
        match = self.state.api_error_once.pop(f"{self.command} {self.path}", None)
        if match is None:
            match = self.state.api_error_once.pop(path, None)
        if match is None:
            return False
        error_code, message = match
        self._send_json(200, {"errorCode": error_code, "msg": message})
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record_request()

        if path == "/feed/status.json":
            self._send_json(200, clone(self.state.status_payload))
            return

        if path == "/feed/ip_list.txt":
            self._send_text(200, "\n".join(self.state.ip_lines) + "\n")
            return

        if not self._authorized():
            self._send_json(401, {"errorCode": -44113, "msg": "Unauthorized"})
            return
        if self._maybe_send_injected_error(path):
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites":
            self._send_json(200, {"errorCode": 0, "result": {"data": clone(self.state.sites)}})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/profiles/groups":
            self._send_json(200, {"errorCode": 0, "result": clone(self.state.groups)})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/routing/policy-routings":
            self._send_json(200, {"errorCode": 0, "result": {"data": clone(self.state.policy_routes)}})
            return

        if path in {
            f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/lan-networks",
            f"/openapi/v2/{self.state.expected_omadac_id}/sites/site-1/lan-networks",
            f"/openapi/v3/{self.state.expected_omadac_id}/sites/site-1/lan-networks",
        }:
            self._send_json(200, {"errorCode": 0, "result": {"data": clone(self.state.lan_networks)}})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/qos/gateway/wans":
            self._send_json(200, {"errorCode": 0, "result": {"data": clone(self.state.wans)}})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/vpn/wireguards":
            self._send_json(200, {"errorCode": 0, "result": {"data": clone(self.state.wireguards)}})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/vpn/site-to-site-vpns":
            self._send_json(200, {"errorCode": 0, "result": clone(self.state.site_to_site_vpns)})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/vpn/client-to-site-vpn-clients":
            self._send_json(200, {"errorCode": 0, "result": clone(self.state.client_to_site_vpns)})
            return

        self._send_json(404, {"errorCode": 404, "msg": path})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        self._record_request()

        if path == "/openapi/authorize/token":
            self.state.auth_call_count += 1
            payload = self._read_json()
            if (
                query.get("grant_type") == ["client_credentials"]
                and payload.get("omadacId") == self.state.expected_omadac_id
                and payload.get("client_id") == self.state.expected_client_id
                and payload.get("client_secret") == self.state.expected_client_secret
            ):
                self._send_json(200, {"accessToken": self.state.access_token, "refreshToken": "refresh-token"})
                return
            self._send_json(401, {"errorCode": -44106, "msg": "Invalid credentials"})
            return

        if not self._authorized():
            self._send_json(401, {"errorCode": -44113, "msg": "Unauthorized"})
            return
        if self._maybe_send_injected_error(path):
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/profiles/groups":
            payload = self._read_json()
            self.state.group_counter += 1
            group_id = f"group-{self.state.group_counter}"
            self.state.groups.append({"groupId": group_id, **payload})
            self._send_json(200, {"errorCode": 0, "result": {"id": group_id}})
            return

        if path == f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/routing/policy-routings":
            payload = self._read_json()
            self.state.route_counter += 1
            route_id = f"route-{self.state.route_counter}"
            self.state.policy_routes.append({"id": route_id, **payload})
            self._send_json(200, {"errorCode": 0, "result": None})
            return

        self._send_json(404, {"errorCode": 404, "msg": path})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record_request()
        if not self._authorized():
            self._send_json(401, {"errorCode": -44113, "msg": "Unauthorized"})
            return
        if self._maybe_send_injected_error(path):
            return

        prefix = f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/profiles/groups/0/"
        if path.startswith(prefix):
            group_id = path.removeprefix(prefix)
            payload = self._read_json()
            for index, group in enumerate(self.state.groups):
                if group.get("groupId") == group_id:
                    self.state.groups[index] = {"groupId": group_id, **payload}
                    self._send_json(200, {"errorCode": 0, "result": None})
                    return
            self._send_json(404, {"errorCode": 404, "msg": "group-not-found"})
            return

        self._send_json(404, {"errorCode": 404, "msg": path})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record_request()
        if not self._authorized():
            self._send_json(401, {"errorCode": -44113, "msg": "Unauthorized"})
            return
        if self._maybe_send_injected_error(path):
            return

        prefix = f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/routing/policy-routings/"
        if path.startswith(prefix):
            route_id = path.removeprefix(prefix)
            payload = self._read_json()
            for index, route in enumerate(self.state.policy_routes):
                if route.get("id") == route_id:
                    self.state.policy_routes[index] = {"id": route_id, **payload}
                    self._send_json(200, {"errorCode": 0, "result": None})
                    return
            self._send_json(404, {"errorCode": 404, "msg": "route-not-found"})
            return

        self._send_json(404, {"errorCode": 404, "msg": path})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record_request()
        if not self._authorized():
            self._send_json(401, {"errorCode": -44113, "msg": "Unauthorized"})
            return
        if self._maybe_send_injected_error(path):
            return

        group_prefix = f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/profiles/groups/0/"
        if path.startswith(group_prefix):
            group_id = path.removeprefix(group_prefix)
            self.state.groups = [group for group in self.state.groups if group.get("groupId") != group_id]
            self._send_json(200, {"errorCode": 0, "result": None})
            return

        route_prefix = f"/openapi/v1/{self.state.expected_omadac_id}/sites/site-1/routing/policy-routings/"
        if path.startswith(route_prefix):
            route_id = path.removeprefix(route_prefix)
            self.state.policy_routes = [route for route in self.state.policy_routes if route.get("id") != route_id]
            self._send_json(200, {"errorCode": 0, "result": None})
            return

        self._send_json(404, {"errorCode": 404, "msg": path})


class OmadaServer:
    def __init__(self, state: FakeOmadaState):
        self.state = state

    def __enter__(self) -> "OmadaServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOmadaHandler)
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class OmadaIntegrationTests(unittest.TestCase):
    def _build_config(self, tmpdir: str, server: OmadaServer, **overrides: Any) -> Config:
        config = Config(
            router_type="omada",  # type: ignore[arg-type]
            run_mode="once",
            site="Default",
            route_name="StopLiga",
            omada_base_url=server.base_url,
            omada_client_id="client-id",
            omada_client_secret="client-secret",
            omada_omadac_id="omadac-id",
            omada_target_type="vpn",  # type: ignore[arg-type]
            omada_target="WG Main",
            omada_verify_tls=False,
            status_url=f"{server.base_url}/feed/status.json",
            ip_list_url=f"{server.base_url}/feed/ip_list.txt",
            feed_allow_private_hosts=True,
            state_file=Path(tmpdir) / "state.json",
            lock_file=Path(tmpdir) / "stopliga.lock",
            bootstrap_guard_file=Path(tmpdir) / "bootstrap_guard.json",
        )
        for key, value in overrides.items():
            config = config.__class__(**{**config.__dict__, key: value})
        return config

    def test_omada_sync_creates_groups_and_policy_route(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24", "2.2.2.0/24", "3.3.3.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            lan_networks=[{"id": "lan-1", "name": "LAN"}, {"id": "lan-2", "name": "IoT"}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            result = StopLigaService(config).run_once()

        self.assertTrue(result.created)
        self.assertTrue(result.changed)
        self.assertEqual(result.backend_name, "omada-policy-routing")
        self.assertEqual(len(state.groups), 1)
        self.assertEqual(state.groups[0]["name"], "StopLiga [001]")
        self.assertEqual(
            state.groups[0]["ipList"],
            [{"ip": "1.1.1.0", "mask": 24}, {"ip": "2.2.2.0", "mask": 24}, {"ip": "3.3.3.0", "mask": 24}],
        )
        self.assertEqual(len(state.policy_routes), 1)
        route = state.policy_routes[0]
        self.assertTrue(route["status"])
        self.assertEqual(route["interfaceType"], 4)
        self.assertEqual(route["vpnIds"], ["wg-1"])
        self.assertEqual(route["sourceIds"], ["lan-1", "lan-2"])
        self.assertEqual(route["destinationIds"], [state.groups[0]["groupId"]])
        self.assertEqual(route["protocols"], [256])

    def test_omada_missing_route_with_empty_inactive_feed_is_noop(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": False,
                "state": "inactive",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            result = StopLigaService(config).run_once()

        self.assertFalse(result.changed)
        self.assertFalse(result.created)
        self.assertFalse(result.desired_enabled)
        self.assertEqual(result.desired_destinations, 0)
        self.assertEqual(state.groups, [])
        self.assertEqual(state.policy_routes, [])
        self.assertNotIn(
            "POST /openapi/v1/omadac-id/sites/site-1/routing/policy-routings",
            state.request_log,
        )

    def test_omada_empty_inactive_feed_disables_route_without_clearing_groups(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": False,
                "state": "inactive",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            groups=[
                {
                    "groupId": "group-1",
                    "name": "StopLiga [001]",
                    "type": 0,
                    "ipList": [{"ip": "203.0.113.0", "mask": 24}],
                }
            ],
            policy_routes=[
                {
                    "id": "route-1",
                    "name": "StopLiga",
                    "status": True,
                    "protocols": [256],
                    "backupInterface": False,
                    "sourceType": 0,
                    "sourceIds": ["lan-1"],
                    "destinationType": 1,
                    "destinationIds": ["group-1"],
                    "interfaceType": 4,
                    "vpnIds": ["wg-1"],
                }
            ],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            result = StopLigaService(config).run_once()

        self.assertTrue(result.changed)
        self.assertFalse(result.created)
        self.assertFalse(result.desired_enabled)
        self.assertEqual(result.desired_destinations, 0)
        self.assertEqual(result.current_destinations, 1)
        self.assertEqual(result.removed_destinations, 0)
        self.assertFalse(state.policy_routes[0]["status"])
        self.assertEqual(state.policy_routes[0]["destinationIds"], ["group-1"])
        self.assertEqual(state.groups[0]["ipList"], [{"ip": "203.0.113.0", "mask": 24}])
        self.assertNotIn(
            "DELETE /openapi/v1/omadac-id/sites/site-1/profiles/groups/0/group-1",
            state.request_log,
        )

    def test_omada_sync_updates_existing_groups_and_cleans_extra_managed_group(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["10.0.0.0/24", "20.0.0.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            groups=[
                {"groupId": "group-1", "name": "StopLiga [001]", "type": 0, "ipList": [{"ip": "1.1.1.0", "mask": 24}]},
                {"groupId": "group-2", "name": "StopLiga [002]", "type": 0, "ipList": [{"ip": "2.2.2.0", "mask": 24}]},
                {"groupId": "group-3", "name": "StopLiga [003]", "type": 0, "ipList": [{"ip": "3.3.3.0", "mask": 24}]},
            ],
            policy_routes=[
                {
                    "id": "route-1",
                    "name": "StopLiga",
                    "status": True,
                    "protocols": [256],
                    "backupInterface": False,
                    "sourceType": 0,
                    "sourceIds": ["lan-1"],
                    "destinationType": 1,
                    "destinationIds": ["group-1", "group-2"],
                    "interfaceType": 4,
                    "vpnIds": ["wg-1"],
                }
            ],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server, omada_group_size=1)
            result = StopLigaService(config).run_once()

        self.assertFalse(result.created)
        self.assertTrue(result.changed)
        self.assertEqual([group["groupId"] for group in state.groups], ["group-1", "group-2"])
        self.assertEqual(state.groups[0]["ipList"], [{"ip": "10.0.0.0", "mask": 24}])
        self.assertEqual(state.groups[1]["ipList"], [{"ip": "20.0.0.0", "mask": 24}])
        self.assertEqual(len(state.policy_routes), 1)
        self.assertTrue(state.policy_routes[0]["status"])

    def test_omada_noop_skips_refresh_verification_reads(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            groups=[
                {"groupId": "group-1", "name": "StopLiga [001]", "type": 0, "ipList": [{"ip": "1.1.1.0", "mask": 24}]}
            ],
            policy_routes=[
                {
                    "id": "route-1",
                    "name": "StopLiga",
                    "status": True,
                    "protocols": [256],
                    "backupInterface": False,
                    "sourceType": 0,
                    "sourceIds": ["lan-1"],
                    "destinationType": 1,
                    "destinationIds": ["group-1"],
                    "interfaceType": 4,
                    "vpnIds": ["wg-1"],
                }
            ],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            result = StopLigaService(config).run_once()

        self.assertFalse(result.created)
        self.assertFalse(result.changed)
        self.assertEqual(
            state.request_log.count("GET /openapi/v1/omadac-id/sites/site-1/profiles/groups"),
            1,
        )
        self.assertEqual(
            state.request_log.count(
                "GET /openapi/v1/omadac-id/sites/site-1/routing/policy-routings?page=1&pageSize=1000"
            ),
            1,
        )

    def test_omada_reauthenticates_when_api_reports_expired_access_token(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            api_error_once={
                "GET /openapi/v1/omadac-id/sites?page=1&pageSize=1000": (
                    -44112,
                    "The access token has expired. Please re-initiate the refreshToken process to obtain the access token.",
                )
            },
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            result = StopLigaService(config).run_once()

        self.assertTrue(result.changed)
        self.assertEqual(state.auth_call_count, 2)

    def test_omada_supports_wan_targets(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            wans=[{"id": "wan-1", "name": "WAN1"}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(
                tmpdir,
                server,
                omada_target_type="wan",
                omada_target="WAN1",
            )
            StopLigaService(config).run_once()

        route = state.policy_routes[0]
        self.assertEqual(route["interfaceType"], 0)
        self.assertEqual(route["interfaceId"], "wan-1")
        self.assertNotIn("vpnIds", route)

    def test_omada_prefers_non_deprecated_vpn_endpoints_before_wireguard_fallback(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            site_to_site_vpns=[{"id": "vpn-1", "name": "WG Main", "status": True}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            StopLigaService(config).run_once()

        self.assertIn("GET /openapi/v1/omadac-id/sites/site-1/vpn/site-to-site-vpns", state.request_log)
        self.assertNotIn("GET /openapi/v1/omadac-id/sites/site-1/vpn/client-to-site-vpn-clients", state.request_log)
        self.assertNotIn(
            "GET /openapi/v1/omadac-id/sites/site-1/vpn/wireguards?page=1&pageSize=1000", state.request_log
        )

    def test_omada_prefers_v3_lan_network_endpoint(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            StopLigaService(config).run_once()

        self.assertIn("GET /openapi/v3/omadac-id/sites/site-1/lan-networks?page=1&pageSize=1000", state.request_log)
        self.assertNotIn("GET /openapi/v1/omadac-id/sites/site-1/lan-networks?page=1&pageSize=1000", state.request_log)

    def test_omada_rejects_ambiguous_source_network_names(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            lan_networks=[{"id": "lan-1", "name": "LAN"}, {"id": "lan-2", "name": "LAN"}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server, omada_source_networks=("LAN",))
            with self.assertRaises(DiscoveryError):
                StopLigaService(config).run_once()

    def test_omada_surfaces_permission_errors_from_open_api(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["1.1.1.0/24"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
            api_error_once={
                "/openapi/v1/omadac-id/sites/site-1/routing/policy-routings": (-1005, "Operation forbidden.")
            },
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            with self.assertRaises(RemoteRequestError):
                StopLigaService(config).run_once()

    def test_omada_rejects_ipv6_destinations(self) -> None:
        state = FakeOmadaState(
            status_payload={
                "lastChangeAt": "2026-04-21 12:00:00",
                "lastChangeEpoch": 1776772800,
                "isBlocked": True,
                "state": "blocked",
            },
            ip_lines=["2001:db8::/32"],
            wireguards=[{"id": "wg-1", "name": "WG Main", "status": True}],
        )
        with OmadaServer(state) as server, tempfile.TemporaryDirectory() as tmpdir:
            config = self._build_config(tmpdir, server)
            with self.assertRaises(UnsupportedRouteShapeError):
                StopLigaService(config).run_once()


class OmadaPerformanceTests(unittest.TestCase):
    def test_empty_inactive_sync_skips_target_and_source_discovery(self) -> None:
        calls: list[str] = []

        class FakeClient:
            def __init__(self, config: Config):
                del config

            def resolve_site(self) -> OmadaSite:
                calls.append("resolve_site")
                return OmadaSite(site_id="site-1", name="Default")

            def list_groups(self, site_id: str) -> list[dict[str, Any]]:
                calls.append(f"list_groups:{site_id}")
                return []

            def list_policy_routes(self, site_id: str) -> list[dict[str, Any]]:
                calls.append(f"list_policy_routes:{site_id}")
                return []

            def list_site_to_site_vpns(self, site_id: str) -> list[dict[str, Any]]:
                calls.append(f"list_site_to_site_vpns:{site_id}")
                return []

            def list_client_to_site_vpns(self, site_id: str) -> list[dict[str, Any]]:
                calls.append(f"list_client_to_site_vpns:{site_id}")
                return []

            def list_wireguard_vpns(self, site_id: str) -> list[dict[str, Any]]:
                calls.append(f"list_wireguard_vpns:{site_id}")
                return []

            def list_lan_networks(self, site_id: str) -> list[dict[str, Any]]:
                calls.append(f"list_lan_networks:{site_id}")
                return []

        config = Config(
            router_type="omada",
            site="Default",
            route_name="StopLiga",
            omada_base_url="https://controller.example",
            omada_client_id="client-id",
            omada_client_secret="client-secret",
            omada_omadac_id="omadac-id",
            omada_target_type="vpn",
            omada_target="WG Main",
        )
        snapshot = FeedSnapshot(
            is_blocked=False,
            desired_enabled=False,
            destinations=[],
            raw_status={},
            raw_line_count=0,
            valid_count=0,
            invalid_count=0,
            invalid_entries=[],
            destinations_hash="destinations",
            feed_hash="feed",
        )

        with patch("stopliga.routers.omada.OmadaClient", FakeClient):
            result = OmadaRouterDriver(config).sync(
                snapshot,
                {},
                guard_writer=lambda *args, **kwargs: None,
                guard_clearer=lambda *args, **kwargs: None,
            )

        self.assertFalse(result.changed)
        self.assertEqual(
            calls,
            ["resolve_site", "list_groups:site-1", "list_policy_routes:site-1"],
        )
