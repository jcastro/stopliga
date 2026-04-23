from __future__ import annotations

import base64
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
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config  # noqa: E402
from stopliga.service import StopLigaService  # noqa: E402


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def ros_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass
class FakeMikroTikState:
    status_payload: dict[str, Any]
    ip_lines: list[str]
    expected_username: str = "admin"
    expected_password: str = "secret"
    routing_tables: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    mangle_rules: list[dict[str, Any]] = field(default_factory=list)
    address_list_entries: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[str] = field(default_factory=list)
    request_bodies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    next_id_value: int = 1

    def next_id(self) -> str:
        value = f"*{self.next_id_value}"
        self.next_id_value += 1
        return value


class FakeMikroTikHandler(BaseHTTPRequestHandler):
    server_version = "FakeMikroTik/1.0"

    @property
    def state(self) -> FakeMikroTikState:
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

    def _record(self) -> None:
        self.state.request_log.append(f"{self.command} {self.path}")

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(
            f"{self.state.expected_username}:{self.state.expected_password}".encode("utf-8")
        ).decode("ascii")
        return self.headers.get("Authorization") == expected

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        self.state.request_bodies.append((self.path, clone(payload)))
        return payload

    def _matches(self, record: dict[str, Any], query: dict[str, list[str]]) -> bool:
        for key, values in query.items():
            if record.get(key) != values[0]:
                return False
        return True

    def _records_for_path(self, path: str) -> list[dict[str, Any]]:
        if path == "/rest/routing/table":
            return self.state.routing_tables
        if path == "/rest/ip/route":
            return self.state.routes
        if path == "/rest/ip/firewall/mangle":
            return self.state.mangle_rules
        if path == "/rest/ip/firewall/address-list":
            return self.state.address_list_entries
        raise AssertionError(f"Unexpected path: {path}")

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/feed/status.json":
            self._send_json(200, clone(self.state.status_payload))
            return

        if path == "/feed/ip_list.txt":
            self._send_text(200, "\n".join(self.state.ip_lines) + "\n")
            return

        if not self._authorized():
            self._send_json(401, {"error": 401, "message": "Unauthorized"})
            return

        records = self._records_for_path(path)
        query = parse_qs(parsed.query)
        filtered = [clone(record) for record in records if self._matches(record, query)]
        self._send_json(200, filtered)

    def do_PUT(self) -> None:  # noqa: N802
        self._record()
        if not self._authorized():
            self._send_json(401, {"error": 401, "message": "Unauthorized"})
            return
        parsed = urlparse(self.path)
        records = self._records_for_path(parsed.path)
        payload = {key: ros_value(value) for key, value in self._read_json().items()}
        created = {".id": self.state.next_id(), **payload}
        records.append(created)
        self._send_json(200, clone(created))

    def do_PATCH(self) -> None:  # noqa: N802
        self._record()
        if not self._authorized():
            self._send_json(401, {"error": 401, "message": "Unauthorized"})
            return
        parsed = urlparse(self.path)
        payload = {key: ros_value(value) for key, value in self._read_json().items()}
        for prefix, records in (
            ("/rest/ip/route/", self.state.routes),
            ("/rest/ip/firewall/mangle/", self.state.mangle_rules),
        ):
            if parsed.path.startswith(prefix):
                item_id = parsed.path.removeprefix(prefix)
                for index, record in enumerate(records):
                    if record.get(".id") == item_id:
                        updated = {**record, **payload}
                        records[index] = updated
                        self._send_json(200, clone(updated))
                        return
        self._send_json(404, {"error": 404, "message": "Not Found"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._record()
        if not self._authorized():
            self._send_json(401, {"error": 401, "message": "Unauthorized"})
            return
        parsed = urlparse(self.path)
        prefix = "/rest/ip/firewall/address-list/"
        if not parsed.path.startswith(prefix):
            self._send_json(404, {"error": 404, "message": "Not Found"})
            return
        item_id = parsed.path.removeprefix(prefix)
        before = len(self.state.address_list_entries)
        self.state.address_list_entries = [
            entry for entry in self.state.address_list_entries if entry.get(".id") != item_id
        ]
        if len(self.state.address_list_entries) == before:
            self._send_json(404, {"error": 404, "message": "Not Found"})
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestServer:
    __test__ = False

    def __init__(self, state: FakeMikroTikState) -> None:
        self.state = state
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.base_url: str | None = None

    def __enter__(self) -> "TestServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeMikroTikHandler)
        self.httpd.state = self.state  # type: ignore[attr-defined]
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
        self.base_url = f"https://{host}:{port}"
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


class MikroTikIntegrationTests(unittest.TestCase):
    def make_config(self, *, state_dir: str, **overrides: Any) -> Config:
        base = {
            "run_mode": "once",
            "router_type": "mikrotik",
            "host": "127.0.0.1",
            "port": 443,
            "site": "default",
            "route_name": "StopLiga",
            "mikrotik_username": "admin",
            "mikrotik_password": "secret",
            "mikrotik_verify_tls": False,
            "mikrotik_routing_table": "vpn_stopliga",
            "mikrotik_gateway": "wireguard-out1",
            "mikrotik_route_distance": 1,
            "mikrotik_address_list": "StopLigaList",
            "mikrotik_in_interface_list": "LAN",
            "feed_verify_tls": False,
            "request_timeout": 5.0,
            "retries": 2,
            "state_file": Path(state_dir) / "state.json",
            "lock_file": Path(state_dir) / "stopliga.lock",
            "bootstrap_guard_file": Path(state_dir) / "bootstrap_guard.json",
            "status_url": "https://invalid/feed/status.json",
            "ip_list_url": "https://invalid/feed/ip_list.txt",
        }
        base.update(overrides)
        return Config(**base)

    def test_service_creates_managed_routeros_objects(self) -> None:
        state = FakeMikroTikState(
            status_payload={"blocked": True},
            ip_lines=["1.1.1.1", "2.2.2.0/24"],
        )
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state) as server:
            assert server.base_url is not None
            config = self.make_config(
                state_dir=tmpdir,
                port=int(server.base_url.rsplit(":", 1)[1]),
                status_url=f"{server.base_url}/feed/status.json",
                ip_list_url=f"{server.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()

        self.assertEqual(result.backend_name, "mikrotik-address-list-routing")
        self.assertTrue(result.changed)
        self.assertTrue(result.created)
        self.assertEqual(result.mode, "mikrotik")
        self.assertEqual(result.current_enabled, None)
        self.assertEqual(result.added_destinations, 2)
        self.assertEqual(result.removed_destinations, 0)

        self.assertEqual(len(state.routing_tables), 1)
        self.assertEqual(state.routing_tables[0]["name"], "vpn_stopliga")
        self.assertEqual(state.routing_tables[0]["fib"], "true")

        self.assertEqual(len(state.routes), 1)
        self.assertEqual(state.routes[0]["routing-table"], "vpn_stopliga")
        self.assertEqual(state.routes[0]["gateway"], "wireguard-out1")
        self.assertEqual(state.routes[0]["dst-address"], "0.0.0.0/0")

        self.assertEqual(len(state.mangle_rules), 1)
        self.assertEqual(state.mangle_rules[0]["action"], "mark-routing")
        self.assertEqual(state.mangle_rules[0]["dst-address-list"], "StopLigaList")
        self.assertEqual(state.mangle_rules[0]["new-routing-mark"], "vpn_stopliga")
        self.assertEqual(state.mangle_rules[0]["disabled"], "false")
        self.assertEqual(state.mangle_rules[0]["in-interface-list"], "LAN")

        addresses = sorted(entry["address"] for entry in state.address_list_entries)
        self.assertEqual(addresses, ["1.1.1.1", "2.2.2.0/24"])

    def test_service_prunes_extra_entries_and_disables_rule_when_feed_is_inactive(self) -> None:
        state = FakeMikroTikState(
            status_payload={"blocked": False},
            ip_lines=["1.1.1.1"],
            routing_tables=[
                {".id": "*10", "name": "vpn_stopliga", "fib": "true"},
            ],
            routes=[
                {
                    ".id": "*11",
                    "comment": "stopliga:route:StopLiga",
                    "disabled": "false",
                    "distance": "1",
                    "dst-address": "0.0.0.0/0",
                    "gateway": "wireguard-out1",
                    "routing-table": "vpn_stopliga",
                }
            ],
            mangle_rules=[
                {
                    ".id": "*12",
                    "comment": "stopliga:mangle:StopLiga",
                    "chain": "prerouting",
                    "action": "mark-routing",
                    "disabled": "false",
                    "dst-address-list": "StopLigaList",
                    "dst-address-type": "!local",
                    "new-routing-mark": "vpn_stopliga",
                    "passthrough": "false",
                    "in-interface-list": "LAN",
                }
            ],
            address_list_entries=[
                {
                    ".id": "*13",
                    "list": "StopLigaList",
                    "address": "1.1.1.1",
                    "comment": "stopliga:destination:StopLiga",
                },
                {
                    ".id": "*14",
                    "list": "StopLigaList",
                    "address": "1.1.1.1",
                    "comment": "stopliga:destination:StopLiga",
                },
                {
                    ".id": "*15",
                    "list": "StopLigaList",
                    "address": "3.3.3.0/24",
                    "comment": "stopliga:destination:StopLiga",
                },
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir, TestServer(state) as server:
            assert server.base_url is not None
            config = self.make_config(
                state_dir=tmpdir,
                port=int(server.base_url.rsplit(":", 1)[1]),
                status_url=f"{server.base_url}/feed/status.json",
                ip_list_url=f"{server.base_url}/feed/ip_list.txt",
            )
            result = StopLigaService(config).run_once()

        self.assertTrue(result.changed)
        self.assertFalse(result.created)
        self.assertEqual(result.current_enabled, True)
        self.assertEqual(result.added_destinations, 0)
        self.assertEqual(result.removed_destinations, 1)
        self.assertEqual(len(state.address_list_entries), 1)
        self.assertEqual(state.address_list_entries[0]["address"], "1.1.1.1")
        self.assertEqual(state.mangle_rules[0]["disabled"], "true")
