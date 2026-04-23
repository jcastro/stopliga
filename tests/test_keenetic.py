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


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config  # noqa: E402
from stopliga.service import StopLigaService  # noqa: E402


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass
class FakeKeeneticState:
    status_payload: dict[str, Any]
    ip_lines: list[str]
    routes: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[str] = field(default_factory=list)


class FakeKeeneticHandler(BaseHTTPRequestHandler):
    server_version = "FakeKeenetic/1.0"

    @property
    def state(self) -> FakeKeeneticState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

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

    def do_GET(self) -> None:  # noqa: N802
        self.state.request_log.append(f"{self.command} {self.path}")
        parsed = urlparse(self.path)
        if parsed.path == "/feed/status.json":
            self._send_json(200, clone(self.state.status_payload))
            return
        if parsed.path == "/feed/ip_list.txt":
            self._send_text(200, "\n".join(self.state.ip_lines) + "\n")
            return
        if parsed.path == "/rci/ip/route":
            self._send_json(200, clone(self.state.routes))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self.state.request_log.append(f"{self.command} {self.path}")
        parsed = urlparse(self.path)
        if parsed.path != "/rci/ip/route":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        self.state.routes.append(payload)
        self._send_json(200, clone(payload))

    def do_DELETE(self) -> None:  # noqa: N802
        self.state.request_log.append(f"{self.command} {self.path}")
        parsed = urlparse(self.path)
        if parsed.path != "/rci/ip/route":
            self.send_response(404)
            self.end_headers()
            return
        query = parse_qs(parsed.query)
        before = len(self.state.routes)

        def matches(route: dict[str, Any]) -> bool:
            for key, values in query.items():
                if str(route.get(key)) != values[0]:
                    return False
            return True

        self.state.routes = [route for route in self.state.routes if not matches(route)]
        if len(self.state.routes) == before:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestServer:
    __test__ = False

    def __init__(self, state: FakeKeeneticState) -> None:
        self.state = state
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url: str | None = None

    def __enter__(self) -> "TestServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeKeeneticHandler)
        self.httpd.state = self.state  # type: ignore[attr-defined]
        host, port = self.httpd.server_address
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)


class KeeneticIntegrationTests(unittest.TestCase):
    def make_config(self, *, state_dir: str, **overrides: Any) -> Config:
        base = {
            "run_mode": "once",
            "router_type": "keenetic",
            "site": "default",
            "route_name": "StopLiga",
            "keenetic_base_url": "http://127.0.0.1",
            "keenetic_username": "admin",
            "keenetic_password": "secret",
            "keenetic_interface": "Wireguard1",
            "keenetic_gateway": "10.10.10.1",
            "keenetic_auto": True,
            "keenetic_reject": True,
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

    def test_service_creates_routes_when_feed_is_blocked(self) -> None:
        state = FakeKeeneticState(
            status_payload={"blocked": True},
            ip_lines=["1.1.1.1", "2.2.2.0/24"],
        )
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state) as server:
            assert server.base_url is not None
            config = self.make_config(
                state_dir=tmpdir,
                keenetic_base_url=server.base_url,
                status_url=f"{server.base_url}/feed/status.json",
                ip_list_url=f"{server.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()

        self.assertEqual(result.backend_name, "keenetic-static-routes")
        self.assertTrue(result.changed)
        self.assertTrue(result.created)
        self.assertEqual(result.mode, "keenetic")
        self.assertEqual(result.current_enabled, None)
        self.assertEqual(result.added_destinations, 2)
        self.assertEqual(result.removed_destinations, 0)

        self.assertEqual(len(state.routes), 2)
        host_route = next(route for route in state.routes if route.get("host") == "1.1.1.1")
        network_route = next(route for route in state.routes if route.get("network") == "2.2.2.0")
        self.assertEqual(host_route["interface"], "Wireguard1")
        self.assertEqual(host_route["gateway"], "10.10.10.1")
        self.assertTrue(host_route["auto"])
        self.assertTrue(host_route["reject"])
        self.assertEqual(network_route["mask"], "255.255.255.0")

    def test_service_removes_managed_routes_when_feed_is_inactive(self) -> None:
        state = FakeKeeneticState(
            status_payload={"blocked": False},
            ip_lines=["1.1.1.1"],
            routes=[
                {
                    "host": "1.1.1.1",
                    "interface": "Wireguard1",
                    "gateway": "10.10.10.1",
                    "auto": True,
                    "reject": True,
                },
                {
                    "network": "3.3.3.0",
                    "mask": "255.255.255.0",
                    "interface": "Wireguard1",
                    "gateway": "10.10.10.1",
                    "auto": True,
                    "reject": True,
                },
                {
                    "network": "9.9.9.0",
                    "mask": "255.255.255.0",
                    "interface": "Home",
                    "auto": False,
                    "reject": False,
                },
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state) as server:
            assert server.base_url is not None
            config = self.make_config(
                state_dir=tmpdir,
                keenetic_base_url=server.base_url,
                status_url=f"{server.base_url}/feed/status.json",
                ip_list_url=f"{server.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()

        self.assertTrue(result.changed)
        self.assertFalse(result.created)
        self.assertTrue(result.current_enabled)
        self.assertEqual(result.added_destinations, 0)
        self.assertEqual(result.removed_destinations, 2)
        self.assertEqual(len(state.routes), 1)
        self.assertEqual(state.routes[0]["interface"], "Home")
