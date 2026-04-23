from __future__ import annotations

import copy
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config, FeedSnapshot, SyncResult  # noqa: E402
from stopliga.opnsense import parse_alias_content, sync_opnsense  # noqa: E402
from stopliga.service import StopLigaService  # noqa: E402
from stopliga.notifier import _safe_notification_url  # noqa: E402
from stopliga.errors import (  # noqa: E402
    AuthenticationError,
    DiscoveryError,
    NetworkError,
    PartialUpdateError,
    ReconciliationRequiredError,
    StateError,
    UnsupportedRouteShapeError,
)


TEST_API_KEY = "test-api-key"


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass
class FakeState:
    status_payload: dict[str, Any]
    ip_lines: list[str]
    route: dict[str, Any] | None
    linked_list: dict[str, Any] | None = None
    network_site_name: str = "default"
    site_id: str = "site-1"
    networks: list[dict[str, Any]] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[str] = field(default_factory=list)
    request_bodies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_route_counter: int = 0
    reject_all_clients_targets: bool = False
    required_api_key: str | None = None
    fail_v2_route_list: bool = False
    fail_v2_route_list_after_create: bool = False
    fail_route_update: bool = False
    linked_list_update_calls: int = 0
    fail_linked_list_update_on_calls: tuple[int, ...] = ()
    gotify_messages: list[dict[str, Any]] = field(default_factory=list)
    telegram_messages: list[dict[str, Any]] = field(default_factory=list)


class FakeUniFiHandler(BaseHTTPRequestHandler):
    server_version = "FakeUniFi/2.0"

    @property
    def state(self) -> FakeState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def mode(self) -> str:
        return self.server.mode  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _record(self) -> None:
        self.state.request_log.append(f"{self.command} {self.path}")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        self.state.request_bodies.append((self.path, clone(payload)))
        return payload

    def _send_json(self, status: int, payload: Any, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
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
        required_api_key = self.state.required_api_key
        if required_api_key is None:
            return True
        return self.headers.get("X-API-Key") == required_api_key

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        parsed = urlparse(self.path)
        path = parsed.path

        if path == f"/proxy/network/v2/api/site/{self.state.network_site_name}/trafficroutes":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            payload = self._read_json()
            if self.state.reject_all_clients_targets and payload.get("target_devices") == [{"type": "ALL_CLIENTS"}]:
                self._send_json(
                    400,
                    {
                        "errorCode": 400,
                        "message": "Validation failed",
                        "detail": "targetDevices ALL_CLIENTS rejected",
                    },
                )
                return
            self.state.created_route_counter += 1
            self.state.route = {"_id": f"created-{self.state.created_route_counter}", **payload}
            if self.state.fail_v2_route_list_after_create:
                self.state.fail_v2_route_list = True
            self._send_json(201, clone(self.state.route))
            return

        if path == "/gotify/message":
            payload = self._read_json()
            self.state.gotify_messages.append(payload)
            self._send_json(200, {"id": len(self.state.gotify_messages)})
            return

        if path.startswith("/telegram/bot") and path.endswith("/sendMessage"):
            payload = self._read_json()
            self.state.telegram_messages.append(payload)
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "not-found", "path": parsed.path})

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        parsed = urlparse(self.path)
        path = parsed.path

        if parsed.path == "/feed/status.json":
            self._send_json(200, clone(self.state.status_payload))
            return

        if parsed.path == "/feed/ip_list.txt":
            self._send_text(200, "\n".join(self.state.ip_lines) + "\n")
            return

        if path == "/proxy/network/api/self/sites":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(
                200,
                {"data": [{"name": self.state.network_site_name, "desc": "Default", "_id": self.state.site_id}]},
            )
            return

        if path in {"/proxy/network/integration/v1/sites", "/proxy/network/v1/sites"}:
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(
                200,
                {
                    "data": [
                        {
                            "siteId": self.state.site_id,
                            "meta": {"name": self.state.network_site_name, "desc": "Default"},
                        }
                    ]
                },
            )
            return

        if path == f"/proxy/network/api/s/{self.state.network_site_name}/rest/networkconf":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, {"data": clone(self.state.networks)})
            return

        if path == f"/proxy/network/api/s/{self.state.network_site_name}/stat/sta":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, {"data": clone(self.state.clients)})
            return

        if path == f"/proxy/network/v2/api/site/{self.state.network_site_name}/trafficroutes":
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            if self.state.fail_v2_route_list:
                self._send_json(500, {"error": "backend-failure"})
                return
            self._send_json(200, {"data": [clone(self.state.route)] if self.state.route else []})
            return

        if self.state.linked_list and path in {
            f"/proxy/network/integration/v1/sites/{self.state.site_id}/traffic-matching-lists/{self.state.linked_list['_id']}",
            f"/proxy/network/v1/sites/{self.state.site_id}/traffic-matching-lists/{self.state.linked_list['_id']}",
        }:
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, clone(self.state.linked_list))
            return

        self._send_json(404, {"error": "not-found", "path": parsed.path})

    def do_PUT(self) -> None:  # noqa: N802
        self._record()
        parsed = urlparse(self.path)
        path = parsed.path

        if (
            self.state.route
            and path
            == f"/proxy/network/v2/api/site/{self.state.network_site_name}/trafficroutes/{self.state.route['_id']}"
        ):
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            if self.state.fail_route_update:
                self._send_json(500, {"error": "update-failed"})
                return
            self.state.route = {"_id": self.state.route["_id"], **self._read_json()}
            self._send_json(200, clone(self.state.route))
            return

        if (
            self.state.linked_list
            and path
            == f"/proxy/network/integration/v1/sites/{self.state.site_id}/traffic-matching-lists/{self.state.linked_list['_id']}"
        ):
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self.state.linked_list_update_calls += 1
            if self.state.linked_list_update_calls in self.state.fail_linked_list_update_on_calls:
                self._send_json(500, {"error": "linked-list-update-failed"})
                return
            payload = self._read_json()
            self.state.linked_list = {"_id": self.state.linked_list["_id"], **payload}
            self._send_json(200, clone(self.state.linked_list))
            return

        self._send_json(404, {"error": "not-found", "path": parsed.path})


class TestServer:
    __test__ = False

    def __init__(self, state: FakeState, *, https: bool) -> None:
        self.state = state
        self.https = https
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.base_url: str | None = None

    def __enter__(self) -> "TestServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeUniFiHandler)
        self.httpd.state = self.state  # type: ignore[attr-defined]

        if self.https:
            if not shutil.which("openssl"):
                raise unittest.SkipTest("openssl is not available")
            self.tempdir = tempfile.TemporaryDirectory()
            cert_path = Path(self.tempdir.name) / "cert.pem"
            key_path = Path(self.tempdir.name) / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key_path),
                    "-out",
                    str(cert_path),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=127.0.0.1",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)

        host, port = self.httpd.server_address
        scheme = "https" if self.https else "http"
        self.base_url = f"{scheme}://{host}:{port}"

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        if self.tempdir:
            self.tempdir.cleanup()


class SingleIterationStopEvent:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return True


class TwoIterationStopEvent:
    def __init__(self) -> None:
        self.wait_calls = 0

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls += 1
        return self.wait_calls >= 2


class ServiceIntegrationTests(unittest.TestCase):
    def make_config(self, *, state_dir: str, **overrides: Any) -> Config:
        base = {
            "run_mode": "once",
            "host": "127.0.0.1",
            "port": 443,
            "api_key": TEST_API_KEY,
            "site": "default",
            "route_name": "LaLiga",
            "unifi_verify_tls": False,
            "feed_verify_tls": False,
            "request_timeout": 5.0,
            "retries": 2,
            "state_file": Path(state_dir) / "state.json",
            "lock_file": Path(state_dir) / "stopliga.lock",
            "bootstrap_guard_file": Path(state_dir) / "bootstrap_guard.json",
            "status_url": "http://invalid/feed/status.json",
            "ip_list_url": "http://invalid/feed/ip_list.txt",
        }
        base.update(overrides)
        return Config(**base)

    def test_local_mode_updates_route(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                host="127.0.0.1",
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.changed)
            self.assertTrue(state.route["enabled"])
            self.assertEqual(
                [item["ip_or_subnet"] for item in state.route["ip_addresses"]],
                ["192.0.2.10", "198.51.100.0/24"],
            )

    def test_feed_failure_writes_error_state_in_once_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.make_config(
                state_dir=tmpdir,
                status_url="http://127.0.0.1:1/feed/status.json",
                ip_list_url="http://127.0.0.1:1/feed/ip_list.txt",
                request_timeout=0.2,
                retries=1,
            )
            with self.assertRaises(NetworkError):
                StopLigaService(config).run_once()
            payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["consecutive_failures"], 1)

    def test_feed_failure_in_loop_mode_counts_once_per_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.make_config(
                state_dir=tmpdir,
                run_mode="loop",
                interval_seconds=1,
                status_url="http://127.0.0.1:1/feed/status.json",
                ip_list_url="http://127.0.0.1:1/feed/ip_list.txt",
                request_timeout=0.2,
                retries=1,
            )
            exit_code = StopLigaService(config).run_loop(SingleIterationStopEvent())
            self.assertEqual(exit_code, 0)
            payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["consecutive_failures"], 1)
            self.assertTrue(payload["last_sync_id"])

    def test_loop_mode_sends_startup_notification_once(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.10", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"last_is_blocked": False}), encoding="utf-8")
            config = self.make_config(
                state_dir=tmpdir,
                run_mode="loop",
                interval_seconds=1,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                gotify_url=f"{feed.base_url}/gotify",
                gotify_token="gotify-token",
            )
            exit_code = StopLigaService(config).run_loop(TwoIterationStopEvent())
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(state.gotify_messages), 1)
            self.assertEqual(state.gotify_messages[0]["title"], "StopLiga Startup")
            self.assertIn("Startup notification test", state.gotify_messages[0]["message"])
            self.assertIn("Providers: Gotify", state.gotify_messages[0]["message"])

    def test_startup_notification_failure_does_not_block_loop(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.10", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                run_mode="loop",
                interval_seconds=1,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                gotify_url="https://gotify.example",
                gotify_token="gotify-token",
            )
            import stopliga.notifier as notifier  # noqa: WPS433

            original_post_json = notifier._post_json

            def fake_post_json(
                url: str,
                payload: dict[str, Any],
                *,
                timeout: float,
                retries: int,
                verify_tls: bool,
                ca_file: Any,
            ) -> None:
                raise NetworkError("gotify down")

            notifier._post_json = fake_post_json
            try:
                exit_code = StopLigaService(config).run_loop(SingleIterationStopEvent())
            finally:
                notifier._post_json = original_post_json

            self.assertEqual(exit_code, 0)
            payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "success")

    def test_local_mode_updates_linked_traffic_matching_list(self) -> None:
        state = FakeState(
            status_payload={"blocked": True},
            ip_lines=["192.0.2.10", "192.0.2.10", "198.51.100.0/24"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "destinationTrafficMatchingListId": "tml-1",
            },
            linked_list={
                "_id": "tml-1",
                "type": "IPV4_ADDRESSES",
                "name": "LaLiga Destinations",
                "items": ["203.0.113.0/24"],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.changed)
            self.assertEqual(state.linked_list["items"], ["192.0.2.10", "198.51.100.0/24"])
            self.assertTrue(state.route["enabled"])

    def test_create_route_from_vpn_and_targets(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            route=None,
            networks=[
                {"_id": "vpn-network-1", "name": "Mullvad DE", "purpose": "vpn-client"},
                {"_id": "lan-1", "name": "Default", "purpose": "corporate"},
            ],
            clients=[
                {"hostname": "apple-tv", "mac": "AA-BB-CC-DD-EE-01"},
                {"name": "salon", "mac": "aa:bb:cc:dd:ee:02"},
            ],
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                vpn_name="Mullvad DE",
                target_clients=("apple-tv", "aa:bb:cc:dd:ee:02"),
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.created)
            self.assertIsNotNone(state.route)
            self.assertEqual(state.route["description"], "LaLiga")
            self.assertTrue(state.route["enabled"])
            self.assertEqual(state.route["network_id"], "vpn-network-1")
            self.assertEqual(
                state.route["target_devices"],
                [
                    {"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"},
                    {"client_mac": "aa:bb:cc:dd:ee:02", "type": "CLIENT"},
                ],
            )
            self.assertEqual(
                [item["ip_or_subnet"] for item in state.route["ip_addresses"]],
                ["192.0.2.10", "198.51.100.0/24"],
            )

    def test_create_route_from_first_available_vpn_with_any_source(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            route=None,
            networks=[
                {"_id": "vpn-network-2", "name": "Zeta VPN", "purpose": "vpn-client"},
                {"_id": "vpn-network-1", "name": "Alpha VPN", "purpose": "vpn-client"},
            ],
            clients=[
                {"hostname": "z-device", "mac": "aa:bb:cc:dd:ee:09"},
                {"hostname": "a-device", "mac": "aa:bb:cc:dd:ee:01"},
            ],
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.created)
            self.assertIsNotNone(state.route)
            self.assertEqual(state.route["description"], "LaLiga")
            self.assertTrue(state.route["enabled"])
            self.assertEqual(state.route["network_id"], "vpn-network-1")
            self.assertEqual(state.route["target_devices"], [{"type": "ALL_CLIENTS"}])
            self.assertEqual(
                [item["ip_or_subnet"] for item in state.route["ip_addresses"]],
                ["192.0.2.10", "198.51.100.0/24"],
            )
            self.assertEqual(result.bootstrap_source, "auto-bootstrap")

    def test_missing_vpn_client_network_points_to_docs(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route=None,
            networks=[],
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertLogs("stopliga.service", level="ERROR") as captured:
                with self.assertRaisesRegex(
                    DiscoveryError,
                    r"https://github\.com/jcastro/stopliga/blob/main/README\.md#vpn-client-network-required",
                ):
                    StopLigaService(config).run_once()
            self.assertTrue(any("vpn_client_network_missing" in line for line in captured.output))

    def test_backend_failures_do_not_trigger_bootstrap(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route=None,
            fail_v2_route_list=True,
            networks=[
                {"_id": "vpn-network-1", "name": "Alpha VPN", "purpose": "vpn-client"},
            ],
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(DiscoveryError):
                StopLigaService(config).run_once()
            self.assertIsNone(state.route)
            self.assertNotIn(
                f"POST /proxy/network/v2/api/site/{state.network_site_name}/trafficroutes", state.request_log
            )

    def test_create_route_falls_back_to_first_device_when_all_clients_target_is_rejected(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            route=None,
            reject_all_clients_targets=True,
            networks=[
                {"_id": "vpn-network-2", "name": "Zeta VPN", "purpose": "vpn-client"},
                {"_id": "vpn-network-1", "name": "Alpha VPN", "purpose": "vpn-client"},
            ],
            clients=[
                {"hostname": "z-device", "mac": "aa:bb:cc:dd:ee:09"},
                {"hostname": "a-device", "mac": "aa:bb:cc:dd:ee:01"},
            ],
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.created)
            self.assertFalse(state.route["enabled"])
            self.assertEqual(state.route["target_devices"], [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}])
            self.assertEqual(result.bootstrap_source, "auto-bootstrap-device-fallback")

    def test_bootstrap_uses_resolved_internal_site_name_for_legacy_endpoints(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route=None,
            networks=[
                {"_id": "vpn-network-1", "name": "Alpha VPN", "purpose": "vpn-client"},
            ],
            site_id="site-1",
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                site="site-1",
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.created)
            self.assertIn("GET /proxy/network/api/s/default/rest/networkconf", state.request_log)

    def test_existing_route_preserves_user_changed_targets(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"type": "ALL_CLIENTS"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "last_success_at": "2099-01-01T00:00:00+00:00",
                        "bootstrap_source": "auto-bootstrap-device-fallback",
                        "bootstrap_network_id": "vpn-network-1",
                        "bootstrap_target_macs": ["aa:bb:cc:dd:ee:01"],
                    }
                ),
                encoding="utf-8",
            )
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.changed)
            self.assertTrue(state.route["enabled"])
            self.assertEqual(state.route["target_devices"], [{"type": "ALL_CLIENTS"}])

    def test_invalid_state_file_is_quarantined_and_sync_continues(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text("{this-is-not-json", encoding="utf-8")
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.changed)
            quarantined = list(Path(tmpdir).glob("state.json.bad-*"))
            self.assertEqual(len(quarantined), 1)

    def test_corrupt_runtime_state_does_not_drop_bootstrap_guard(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"type": "ALL_CLIENTS"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text("{this-is-not-json", encoding="utf-8")
            guard_path = Path(tmpdir) / "bootstrap_guard.json"
            guard_path.write_text(
                json.dumps(
                    {
                        "status": "guard",
                        "bootstrap_source": "auto-bootstrap",
                        "bootstrap_network_id": "vpn-network-1",
                        "bootstrap_target_macs": ["__all_clients__"],
                    }
                ),
                encoding="utf-8",
            )
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                bootstrap_guard_file=guard_path,
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.changed)
            self.assertTrue(state.route["enabled"])

    def test_bootstrap_does_not_relist_routes_after_create(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route=None,
            networks=[{"_id": "vpn-network-1", "name": "Mullvad DE", "purpose": "vpn-client"}],
            clients=[{"hostname": "apple-tv", "mac": "aa:bb:cc:dd:ee:01"}],
            fail_v2_route_list_after_create=True,
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.created)
            self.assertEqual(result.route_id, "created-1")
            self.assertEqual(
                state.request_log.count(f"GET /proxy/network/v2/api/site/{state.network_site_name}/trafficroutes"),
                2,
            )

    def test_bootstrap_guard_is_persisted_before_state_write(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route=None,
            networks=[{"_id": "vpn-network-1", "name": "Alpha VPN", "purpose": "vpn-client"}],
            clients=[{"hostname": "a-device", "mac": "aa:bb:cc:dd:ee:01"}],
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                state_file=Path(tmpdir),
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(StateError):
                StopLigaService(config).run_once()
            guard_payload = json.loads((Path(tmpdir) / "bootstrap_guard.json").read_text(encoding="utf-8"))
            self.assertEqual(guard_payload["bootstrap_source"], "auto-bootstrap")
            self.assertEqual(guard_payload["bootstrap_network_id"], "vpn-network-1")
            self.assertEqual(guard_payload["bootstrap_target_macs"], ["__all_clients__"])

    def test_linked_list_rejects_wrong_ip_family(self) -> None:
        state = FakeState(
            status_payload={"blocked": True},
            ip_lines=["2001:db8::/32"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "destinationTrafficMatchingListId": "tml-1",
            },
            linked_list={
                "_id": "tml-1",
                "type": "IPV4_ADDRESSES",
                "name": "LaLiga Destinations",
                "items": ["203.0.113.0/24"],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(UnsupportedRouteShapeError):
                StopLigaService(config).run_once()

    def test_route_update_sends_minimal_payload_without_internal_fields(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
                "computed_status": "internal-only",
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            StopLigaService(config).run_once()
            route_puts = [body for path, body in state.request_bodies if path.endswith("/trafficroutes/route-1")]
            self.assertEqual(len(route_puts), 1)
            self.assertNotIn("_id", route_puts[0])
            self.assertNotIn("computed_status", route_puts[0])

    def test_invalid_api_key_raises_authentication_error(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            required_api_key="correct-key",
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                api_key="wrong-key",
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(AuthenticationError):
                StopLigaService(config).run_once()

    def test_partial_update_records_failed_stage(self) -> None:
        state = FakeState(
            status_payload={"blocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            fail_route_update=True,
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "destinationTrafficMatchingListId": "tml-1",
            },
            linked_list={
                "_id": "tml-1",
                "type": "IPV4_ADDRESSES",
                "name": "LaLiga Destinations",
                "items": ["203.0.113.0/24"],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(PartialUpdateError) as ctx:
                StopLigaService(config).run_once()
            self.assertEqual(ctx.exception.failed_stage, "route")
            self.assertEqual(ctx.exception.completed_stages, ("linked_list",))
            self.assertTrue(ctx.exception.rollback_attempted)
            self.assertTrue(ctx.exception.rollback_completed)
            self.assertIsNone(ctx.exception.rollback_error)
            self.assertEqual(state.linked_list["items"], ["203.0.113.0/24"])
            state_payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(state_payload["partial_failure"])
            self.assertEqual(state_payload["last_error_stage"], "route")
            self.assertTrue(state_payload["rollback_attempted"])
            self.assertTrue(state_payload["rollback_completed"])
            self.assertIsNone(state_payload["rollback_error"])
            self.assertFalse(state_payload["reconciliation_required"])

    def test_failed_rollback_marks_state_as_requiring_reconciliation(self) -> None:
        state = FakeState(
            status_payload={"blocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            fail_route_update=True,
            fail_linked_list_update_on_calls=(2,),
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "destinationTrafficMatchingListId": "tml-1",
            },
            linked_list={
                "_id": "tml-1",
                "type": "IPV4_ADDRESSES",
                "name": "LaLiga Destinations",
                "items": ["203.0.113.0/24"],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(PartialUpdateError) as ctx:
                StopLigaService(config).run_once()
            self.assertTrue(ctx.exception.rollback_attempted)
            self.assertFalse(ctx.exception.rollback_completed)
            self.assertIsNotNone(ctx.exception.rollback_error)
            state_payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(state_payload["reconciliation_required"])

    def test_reconciliation_required_state_blocks_future_writes(self) -> None:
        state = FakeState(
            status_payload={"blocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "error",
                        "reconciliation_required": True,
                        "last_error_stage": "route",
                        "rollback_completed": False,
                    }
                ),
                encoding="utf-8",
            )
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(ReconciliationRequiredError):
                StopLigaService(config).run_once()
            self.assertNotIn("GET /feed/status.json", state.request_log)

    def test_incomplete_route_is_not_enabled_automatically(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "last_success_at": "2099-01-01T00:00:00+00:00",
                        "bootstrap_source": "auto-bootstrap-device-fallback",
                        "bootstrap_network_id": "vpn-network-1",
                        "bootstrap_target_macs": ["aa:bb:cc:dd:ee:01"],
                    }
                ),
                encoding="utf-8",
            )
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()
            self.assertTrue(result.changed)
            self.assertFalse(state.route["enabled"])

    def test_gotify_notification_is_sent_for_block_and_ip_changes(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10", "198.51.100.0/24"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"last_is_blocked": False}), encoding="utf-8")
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                gotify_url=f"{feed.base_url}/gotify",
                gotify_token="gotify-token",
            )
            StopLigaService(config).run_once()
            self.assertEqual(len(state.gotify_messages), 1)
            self.assertIn("Route: LaLiga", state.gotify_messages[0]["message"])
            self.assertIn("Block status: INACTIVE -> ACTIVE", state.gotify_messages[0]["message"])
            self.assertIn("IP list: +2 added, -1 removed", state.gotify_messages[0]["message"])
            self.assertIn("Blocking: ACTIVE", state.gotify_messages[0]["message"])

    def test_telegram_notification_is_sent_for_block_change(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.10", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"last_is_blocked": True}), encoding="utf-8")
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                telegram_bot_token=f"{feed.base_url}/telegram/bot-token".replace(f"{feed.base_url}/", ""),
                telegram_chat_id="123456",
            )
            import stopliga.notifier as notifier  # noqa: WPS433

            original_post_json = notifier._post_json

            def fake_post_json(
                url: str,
                payload: dict[str, Any],
                *,
                timeout: float,
                retries: int,
                verify_tls: bool,
                ca_file: Any,
            ) -> None:
                state.telegram_messages.append({"url": url, **payload})

            notifier._post_json = fake_post_json
            try:
                StopLigaService(config).run_once()
            finally:
                notifier._post_json = original_post_json

            self.assertEqual(len(state.telegram_messages), 1)
            self.assertEqual(state.telegram_messages[0]["chat_id"], "123456")
            self.assertIn("Route: LaLiga", state.telegram_messages[0]["text"])
            self.assertIn("Block status: ACTIVE -> INACTIVE", state.telegram_messages[0]["text"])
            self.assertIn("Blocking: INACTIVE", state.telegram_messages[0]["text"])

    def test_telegram_notification_can_target_group_topic(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.10", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"last_is_blocked": True}), encoding="utf-8")
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                telegram_bot_token=f"{feed.base_url}/telegram/bot-token".replace(f"{feed.base_url}/", ""),
                telegram_group_id="-1009876543210",
                telegram_topic_id=77,
            )
            import stopliga.notifier as notifier  # noqa: WPS433

            original_post_json = notifier._post_json

            def fake_post_json(
                url: str,
                payload: dict[str, Any],
                *,
                timeout: float,
                retries: int,
                verify_tls: bool,
                ca_file: Any,
            ) -> None:
                state.telegram_messages.append({"url": url, **payload})

            notifier._post_json = fake_post_json
            try:
                StopLigaService(config).run_once()
            finally:
                notifier._post_json = original_post_json

            self.assertEqual(len(state.telegram_messages), 1)
            self.assertEqual(state.telegram_messages[0]["chat_id"], "-1009876543210")
            self.assertEqual(state.telegram_messages[0]["message_thread_id"], 77)
            self.assertIn("Block status: ACTIVE -> INACTIVE", state.telegram_messages[0]["text"])

    def test_notification_error_redacts_telegram_token(self) -> None:
        safe = _safe_notification_url("https://api.telegram.org/bot123456:secret-token/sendMessage")
        self.assertEqual(safe, "https://api.telegram.org/bot***/sendMessage")

    def test_state_records_last_sync_id(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.10", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            StopLigaService(config).run_once()
            state_payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertRegex(state_payload["last_sync_id"], r"^[0-9a-f]{12}$")

    def test_notification_provider_failure_does_not_block_other_providers(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": True},
            ip_lines=["192.0.2.10"],
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "203.0.113.0/24", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"last_is_blocked": False}), encoding="utf-8")
            config = self.make_config(
                state_dir=tmpdir,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
                gotify_url="https://gotify.example",
                gotify_token="gotify-token",
                telegram_bot_token="123456:token",
                telegram_chat_id="123456",
            )
            import stopliga.notifier as notifier  # noqa: WPS433

            original_post_json = notifier._post_json

            def fake_post_json(
                url: str,
                payload: dict[str, Any],
                *,
                timeout: float,
                retries: int,
                verify_tls: bool,
                ca_file: Any,
            ) -> None:
                if "gotify" in url:
                    raise NetworkError("gotify down")
                state.telegram_messages.append({"url": url, **payload})

            notifier._post_json = fake_post_json
            try:
                result = StopLigaService(config).run_once()
            finally:
                notifier._post_json = original_post_json

            self.assertTrue(result.changed)
            self.assertEqual(len(state.telegram_messages), 1)
            self.assertIn("Block status: INACTIVE -> ACTIVE", state.telegram_messages[0]["text"])

    def test_feed_response_size_limit_is_enforced(self) -> None:
        state = FakeState(
            status_payload={"isBlocked": False},
            ip_lines=["192.0.2.1"] * 300,
            route={
                "_id": "route-1",
                "name": "LaLiga",
                "enabled": False,
                "network_id": "vpn-network-1",
                "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.10", "ip_version": "IPv4"}],
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            TestServer(state, https=True) as unifi,
            TestServer(state, https=False) as feed,
        ):
            config = self.make_config(
                state_dir=tmpdir,
                max_response_bytes=1024,
                port=int(unifi.base_url.rsplit(":", 1)[1]),
                status_url=f"{feed.base_url}/feed/status.json",
                ip_list_url=f"{feed.base_url}/feed/ip_list.txt",
            )
            with self.assertRaises(NetworkError):
                StopLigaService(config).run_once()


class OPNsenseTests(unittest.TestCase):
    @staticmethod
    def make_opnsense_config(**overrides: Any) -> Config:
        base = {
            "run_mode": "once",
            "firewall_backend": "opnsense",
            "route_name": "LaLiga",
            "opnsense_host": "fw.local",
            "opnsense_api_key": "test-opnsense-key",
            "opnsense_api_secret": "test-opnsense-secret",
            "opnsense_verify_tls": False,
            "feed_verify_tls": False,
        }
        base.update(overrides)
        return Config(**base)

    @staticmethod
    def make_feed_snapshot(
        *,
        is_blocked: bool = True,
        destinations: list[str] | None = None,
    ) -> FeedSnapshot:
        values = destinations or ["192.0.2.10", "198.51.100.0/24"]
        return FeedSnapshot(
            is_blocked=is_blocked,
            desired_enabled=is_blocked,
            destinations=values,
            raw_status={"isBlocked": is_blocked},
            raw_line_count=len(values),
            valid_count=len(values),
            invalid_count=0,
            invalid_entries=[],
            destinations_hash="destinations-hash",
            feed_hash="feed-hash",
        )

    def test_parse_alias_content_reads_selected_entries_from_getitem_shape(self) -> None:
        alias_record = {
            "content": {
                "192.0.2.10": {"value": "192.0.2.10", "selected": 1},
                "198.51.100.0/24": {"value": "198.51.100.0/24", "selected": 1},
                "OtherAlias": {"value": "OtherAlias", "selected": 0, "description": "ignored"},
            }
        }

        self.assertEqual(parse_alias_content(alias_record), ["192.0.2.10", "198.51.100.0/24"])

    def test_parse_alias_content_accepts_string_selected_flags(self) -> None:
        alias_record = {
            "content": {
                "192.0.2.10": {"value": "192.0.2.10", "selected": "1"},
                "198.51.100.0/24": {"value": "198.51.100.0/24", "selected": "true"},
                "OtherAlias": {"value": "OtherAlias", "selected": "0"},
            }
        }

        self.assertEqual(parse_alias_content(alias_record), ["192.0.2.10", "198.51.100.0/24"])

    def test_sync_opnsense_is_idempotent_when_alias_and_rule_already_match(self) -> None:
        config = self.make_opnsense_config()
        feed_snapshot = self.make_feed_snapshot()
        alias_record = {
            "content": {
                "192.0.2.10": {"value": "192.0.2.10", "selected": 1},
                "198.51.100.0/24": {"value": "198.51.100.0/24", "selected": 1},
                "UnusedAlias": {"value": "UnusedAlias", "selected": 0},
            }
        }

        with (
            patch("stopliga.opnsense.OPNsenseClient.authenticate", return_value=None),
            patch("stopliga.opnsense.OPNsenseClient.search_alias", return_value={"uuid": "alias-1"}),
            patch("stopliga.opnsense.OPNsenseClient.get_alias_item", return_value=alias_record),
            patch("stopliga.opnsense.OPNsenseClient.search_rule", return_value={"uuid": "rule-1", "enabled": "1"}),
            patch("stopliga.opnsense.OPNsenseClient.update_alias_content") as update_alias_content,
            patch("stopliga.opnsense.OPNsenseClient.reconfigure_alias") as reconfigure_alias,
            patch("stopliga.opnsense.OPNsenseClient.toggle_rule") as toggle_rule,
            patch("stopliga.opnsense.OPNsenseClient.apply_filter") as apply_filter,
        ):
            result = sync_opnsense(config, feed_snapshot)

        self.assertFalse(result.changed)
        self.assertFalse(result.created)
        self.assertEqual(result.current_destinations, 2)
        self.assertEqual(result.added_destinations, 0)
        self.assertEqual(result.removed_destinations, 0)
        self.assertTrue(result.current_enabled)
        update_alias_content.assert_not_called()
        reconfigure_alias.assert_not_called()
        toggle_rule.assert_not_called()
        apply_filter.assert_not_called()

    def test_sync_opnsense_updates_alias_and_rule_when_needed(self) -> None:
        config = self.make_opnsense_config()
        feed_snapshot = self.make_feed_snapshot()
        alias_record = {
            "content": {
                "203.0.113.0/24": {"value": "203.0.113.0/24", "selected": 1},
                "UnusedAlias": {"value": "UnusedAlias", "selected": 0},
            }
        }

        with (
            patch("stopliga.opnsense.OPNsenseClient.authenticate", return_value=None),
            patch("stopliga.opnsense.OPNsenseClient.search_alias", return_value={"uuid": "alias-1"}),
            patch("stopliga.opnsense.OPNsenseClient.get_alias_item", return_value=alias_record),
            patch("stopliga.opnsense.OPNsenseClient.search_rule", return_value={"uuid": "rule-1", "enabled": "0"}),
            patch("stopliga.opnsense.OPNsenseClient.update_alias_content") as update_alias_content,
            patch("stopliga.opnsense.OPNsenseClient.reconfigure_alias") as reconfigure_alias,
            patch("stopliga.opnsense.OPNsenseClient.toggle_rule") as toggle_rule,
            patch("stopliga.opnsense.OPNsenseClient.apply_filter") as apply_filter,
        ):
            result = sync_opnsense(config, feed_snapshot)

        self.assertTrue(result.changed)
        self.assertFalse(result.created)
        self.assertFalse(result.current_enabled)
        self.assertEqual(result.current_destinations, 1)
        self.assertEqual(result.added_destinations, 2)
        self.assertEqual(result.removed_destinations, 1)
        update_alias_content.assert_called_once_with("alias-1", "LaLiga", ["192.0.2.10", "198.51.100.0/24"])
        reconfigure_alias.assert_called_once_with()
        toggle_rule.assert_called_once_with("rule-1", True)
        apply_filter.assert_called_once_with()

    def test_service_dispatches_to_opnsense_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.make_opnsense_config(
                request_timeout=5.0,
                retries=2,
                state_file=Path(tmpdir) / "state.json",
                lock_file=Path(tmpdir) / "stopliga.lock",
                bootstrap_guard_file=Path(tmpdir) / "bootstrap_guard.json",
                status_url="http://invalid/feed/status.json",
                ip_list_url="http://invalid/feed/ip_list.txt",
            )
            feed_snapshot = self.make_feed_snapshot()
            expected_result = SyncResult(
                mode="opnsense",
                route_name="LaLiga",
                route_id="rule-1",
                backend_name="opnsense-alias+rule",
                changed=False,
                created=False,
                dry_run=False,
                desired_enabled=True,
                current_enabled=True,
                desired_destinations=2,
                current_destinations=2,
                invalid_entries=0,
                feed_hash="feed-hash",
                destinations_hash="destinations-hash",
                summary="already in sync",
                is_blocked=True,
            )

            with (
                patch("stopliga.service.load_feed_snapshot", return_value=feed_snapshot),
                patch("stopliga.routers.opnsense.sync_opnsense", return_value=expected_result) as sync_opnsense_mock,
            ):
                result = StopLigaService(config).run_once()

        self.assertEqual(result.mode, "opnsense")
        sync_opnsense_mock.assert_called_once_with(config, feed_snapshot)
