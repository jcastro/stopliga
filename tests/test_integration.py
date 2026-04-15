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


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config  # noqa: E402
from stopliga.service import StopLigaService  # noqa: E402


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
    created_route_counter: int = 0


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
        return json.loads(raw.decode("utf-8"))

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

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        parsed = urlparse(self.path)
        path = parsed.path

        if parsed.path == "/api/auth/login":
            payload = self._read_json()
            if payload.get("username") and payload.get("password"):
                self._send_json(200, {"meta": {"rc": "ok"}}, headers={"X-CSRF-Token": "csrf-test-token"})
                return

        if path == f"/proxy/network/v2/api/site/{self.state.network_site_name}/trafficroutes":
            payload = self._read_json()
            self.state.created_route_counter += 1
            self.state.route = {"_id": f"created-{self.state.created_route_counter}", **payload}
            self._send_json(201, clone(self.state.route))
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
            self._send_json(
                200,
                {"data": [{"name": self.state.network_site_name, "desc": "Default", "_id": self.state.site_id}]},
            )
            return

        if path in {"/proxy/network/integration/v1/sites", "/proxy/network/v1/sites"}:
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
            self._send_json(200, {"data": clone(self.state.networks)})
            return

        if path == f"/proxy/network/api/s/{self.state.network_site_name}/stat/sta":
            self._send_json(200, {"data": clone(self.state.clients)})
            return

        if path == f"/proxy/network/v2/api/site/{self.state.network_site_name}/trafficroutes":
            self._send_json(200, {"data": [clone(self.state.route)] if self.state.route else []})
            return

        if (
            self.state.linked_list
            and path
            in {
                f"/proxy/network/integration/v1/sites/{self.state.site_id}/traffic-matching-lists/{self.state.linked_list['_id']}",
                f"/proxy/network/v1/sites/{self.state.site_id}/traffic-matching-lists/{self.state.linked_list['_id']}",
            }
        ):
            self._send_json(200, clone(self.state.linked_list))
            return

        self._send_json(404, {"error": "not-found", "path": parsed.path})

    def do_PUT(self) -> None:  # noqa: N802
        self._record()
        parsed = urlparse(self.path)
        path = parsed.path

        if self.state.route and path == f"/proxy/network/v2/api/site/{self.state.network_site_name}/trafficroutes/{self.state.route['_id']}":
            self.state.route = self._read_json()
            self._send_json(200, clone(self.state.route))
            return

        if (
            self.state.linked_list
            and path
            == f"/proxy/network/integration/v1/sites/{self.state.site_id}/traffic-matching-lists/{self.state.linked_list['_id']}"
        ):
            payload = self._read_json()
            self.state.linked_list = {"_id": self.state.linked_list["_id"], **payload}
            self._send_json(200, clone(self.state.linked_list))
            return

        self._send_json(404, {"error": "not-found", "path": parsed.path})


class TestServer:
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


class ServiceIntegrationTests(unittest.TestCase):
    def make_config(self, *, state_dir: str, **overrides: Any) -> Config:
        base = {
            "run_mode": "once",
            "host": "127.0.0.1",
            "port": 443,
            "username": "admin",
            "password": "password",
            "site": "default",
            "route_name": "LaLiga",
            "unifi_verify_tls": False,
            "feed_verify_tls": False,
            "request_timeout": 5.0,
            "retries": 2,
            "state_file": Path(state_dir) / "state.json",
            "lock_file": Path(state_dir) / "stopliga.lock",
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
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state, https=True) as unifi, TestServer(state, https=False) as feed:
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
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state, https=True) as unifi, TestServer(state, https=False) as feed:
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
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state, https=True) as unifi, TestServer(state, https=False) as feed:
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
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state, https=True) as unifi, TestServer(state, https=False) as feed:
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
            self.assertFalse(state.route["enabled"])
            self.assertEqual(state.route["network_id"], "vpn-network-1")
            self.assertEqual(state.route["target_devices"], [])
            self.assertEqual(
                [item["ip_or_subnet"] for item in state.route["ip_addresses"]],
                ["192.0.2.10", "198.51.100.0/24"],
            )
            self.assertEqual(result.bootstrap_source, "auto-bootstrap")

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
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state, https=True) as unifi, TestServer(state, https=False) as feed:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "last_success_at": "2099-01-01T00:00:00+00:00",
                        "bootstrap_source": "auto-bootstrap",
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
