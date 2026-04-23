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
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config  # noqa: E402
from stopliga.service import StopLigaService  # noqa: E402


SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICE_TYPE = "urn:dslforum-org:service:Layer3Forwarding:1"


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass
class FakeFRITZBoxState:
    status_payload: dict[str, Any]
    ip_lines: list[str]
    default_connection_service: str = "1.WANPPPConnection.1"
    forwarding_entries: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[str] = field(default_factory=list)


class FakeFRITZBoxHandler(BaseHTTPRequestHandler):
    server_version = "FakeFRITZBox/1.0"

    @property
    def state(self) -> FakeFRITZBoxState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_text(self, status: int, content: str, *, content_type: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        self._send_text(status, json.dumps(payload), content_type="application/json")

    def _send_xml_response(self, action: str, fields: dict[str, str]) -> None:
        envelope = ET.Element(f"{{{SOAP_ENV_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
        response = ET.SubElement(body, f"{{{SERVICE_TYPE}}}{action}Response")
        for name, value in fields.items():
            child = ET.SubElement(response, name)
            child.text = value
        self._send_text(
            200,
            ET.tostring(envelope, encoding="unicode"),
            content_type='text/xml; charset="utf-8"',
        )

    def _send_fault(self, message: str) -> None:
        envelope = ET.Element(f"{{{SOAP_ENV_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
        fault = ET.SubElement(body, f"{{{SOAP_ENV_NS}}}Fault")
        fault_string = ET.SubElement(fault, "faultstring")
        fault_string.text = message
        self._send_text(
            500,
            ET.tostring(envelope, encoding="unicode"),
            content_type='text/xml; charset="utf-8"',
        )

    def do_GET(self) -> None:  # noqa: N802
        self.state.request_log.append(f"{self.command} {self.path}")
        if self.path == "/feed/status.json":
            self._send_json(200, clone(self.state.status_payload))
            return
        if self.path == "/feed/ip_list.txt":
            self._send_text(200, "\n".join(self.state.ip_lines) + "\n", content_type="text/plain; charset=utf-8")
            return
        if self.path == "/tr64desc.xml":
            self._send_text(
                200,
                """
<root>
  <device>
    <serviceList>
      <service>
        <serviceType>urn:dslforum-org:service:Layer3Forwarding:1</serviceType>
        <serviceId>urn:Layer3Forwarding-com:serviceId:Layer3Forwarding1</serviceId>
        <controlURL>/upnp/control/layer3forwarding</controlURL>
      </service>
    </serviceList>
  </device>
</root>
                """.strip(),
                content_type="text/xml; charset=utf-8",
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self.state.request_log.append(f"{self.command} {self.path}")
        if self.path != "/upnp/control/layer3forwarding":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        root = ET.fromstring(raw)
        body = next(element for element in root.iter() if element.tag.endswith("Body"))
        action_element = next(iter(body))
        action = action_element.tag.rsplit("}", 1)[-1]
        arguments = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in action_element}

        if action == "GetDefaultConnectionService":
            self._send_xml_response(action, {"NewDefaultConnectionService": self.state.default_connection_service})
            return
        if action == "GetForwardNumberOfEntries":
            self._send_xml_response(action, {"NewForwardNumberOfEntries": str(len(self.state.forwarding_entries))})
            return
        if action == "GetGenericForwardingEntry":
            index = int(arguments["NewForwardingIndex"])
            entry = self.state.forwarding_entries[index]
            self._send_xml_response(
                action,
                {
                    "NewEnable": "1" if entry["enabled"] else "0",
                    "NewStatus": "Enabled" if entry["enabled"] else "Disabled",
                    "NewType": entry["type"],
                    "NewDestIPAddress": entry["dest_ip"],
                    "NewDestSubnetMask": entry["dest_mask"],
                    "NewSourceIPAddress": entry["source_ip"],
                    "NewSourceSubnetMask": entry["source_mask"],
                    "NewGatewayIPAddress": entry["gateway"],
                    "NewInterface": entry["interface"],
                    "NewForwardingMetric": str(entry["metric"]),
                },
            )
            return
        if action == "AddForwardingEntry":
            self.state.forwarding_entries.append(
                {
                    "enabled": True,
                    "type": arguments["NewType"],
                    "dest_ip": arguments["NewDestIPAddress"],
                    "dest_mask": arguments["NewDestSubnetMask"],
                    "source_ip": arguments["NewSourceIPAddress"],
                    "source_mask": arguments["NewSourceSubnetMask"],
                    "gateway": arguments["NewGatewayIPAddress"],
                    "interface": arguments["NewInterface"],
                    "metric": int(arguments["NewForwardingMetric"]),
                }
            )
            self._send_xml_response(action, {})
            return
        if action == "DeleteForwardingEntry":
            before = len(self.state.forwarding_entries)
            self.state.forwarding_entries = [
                entry
                for entry in self.state.forwarding_entries
                if not (
                    entry["dest_ip"] == arguments["NewDestIPAddress"]
                    and entry["dest_mask"] == arguments["NewDestSubnetMask"]
                    and entry["source_ip"] == arguments["NewSourceIPAddress"]
                    and entry["source_mask"] == arguments["NewSourceSubnetMask"]
                )
            ]
            if len(self.state.forwarding_entries) == before:
                self._send_fault("route not found")
                return
            self._send_xml_response(action, {})
            return
        if action == "SetForwardingEntryEnable":
            for entry in self.state.forwarding_entries:
                if (
                    entry["dest_ip"] == arguments["NewDestIPAddress"]
                    and entry["dest_mask"] == arguments["NewDestSubnetMask"]
                    and entry["source_ip"] == arguments["NewSourceIPAddress"]
                    and entry["source_mask"] == arguments["NewSourceSubnetMask"]
                ):
                    entry["enabled"] = arguments["NewEnable"] in {"1", "true", "yes"}
                    self._send_xml_response(action, {})
                    return
            self._send_fault("route not found")
            return

        self._send_fault(f"unsupported action {action}")


class TestServer:
    __test__ = False

    def __init__(self, state: FakeFRITZBoxState) -> None:
        self.state = state
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.base_url: str | None = None

    def __enter__(self) -> "TestServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeFRITZBoxHandler)
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


class FRITZBoxIntegrationTests(unittest.TestCase):
    def make_config(self, *, state_dir: str, **overrides: Any) -> Config:
        base = {
            "run_mode": "once",
            "router_type": "fritzbox",
            "host": "127.0.0.1",
            "port": 443,
            "site": "default",
            "route_name": "StopLiga",
            "fritzbox_username": "stopliga",
            "fritzbox_password": "secret",
            "fritzbox_gateway": "192.168.178.2",
            "fritzbox_route_metric": 4096,
            "fritzbox_verify_tls": False,
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

    def test_service_creates_managed_static_routes(self) -> None:
        state = FakeFRITZBoxState(
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

        self.assertEqual(result.backend_name, "fritzbox-static-routes")
        self.assertTrue(result.changed)
        self.assertTrue(result.created)
        self.assertEqual(result.mode, "fritzbox")
        self.assertEqual(result.current_enabled, None)
        self.assertEqual(result.added_destinations, 2)
        self.assertEqual(result.removed_destinations, 0)

        self.assertEqual(len(state.forwarding_entries), 2)
        routes = {(entry["dest_ip"], entry["dest_mask"]): entry for entry in state.forwarding_entries}
        self.assertEqual(routes[("1.1.1.1", "255.255.255.255")]["type"], "Host")
        self.assertEqual(routes[("2.2.2.0", "255.255.255.0")]["type"], "Network")
        for entry in state.forwarding_entries:
            self.assertTrue(entry["enabled"])
            self.assertEqual(entry["gateway"], "192.168.178.2")
            self.assertEqual(entry["interface"], "1.WANPPPConnection.1")
            self.assertEqual(entry["metric"], 4096)

    def test_service_prunes_extra_routes_and_disables_managed_set(self) -> None:
        state = FakeFRITZBoxState(
            status_payload={"blocked": False},
            ip_lines=["1.1.1.1"],
            forwarding_entries=[
                {
                    "enabled": True,
                    "type": "Host",
                    "dest_ip": "1.1.1.1",
                    "dest_mask": "255.255.255.255",
                    "source_ip": "0.0.0.0",
                    "source_mask": "0.0.0.0",
                    "gateway": "192.168.178.2",
                    "interface": "1.WANPPPConnection.1",
                    "metric": 4096,
                },
                {
                    "enabled": True,
                    "type": "Network",
                    "dest_ip": "3.3.3.0",
                    "dest_mask": "255.255.255.0",
                    "source_ip": "0.0.0.0",
                    "source_mask": "0.0.0.0",
                    "gateway": "192.168.178.2",
                    "interface": "1.WANPPPConnection.1",
                    "metric": 4096,
                },
                {
                    "enabled": True,
                    "type": "Network",
                    "dest_ip": "9.9.9.0",
                    "dest_mask": "255.255.255.0",
                    "source_ip": "0.0.0.0",
                    "source_mask": "0.0.0.0",
                    "gateway": "192.168.178.9",
                    "interface": "1.WANPPPConnection.1",
                    "metric": 100,
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
        self.assertTrue(result.current_enabled)
        self.assertEqual(result.added_destinations, 0)
        self.assertEqual(result.removed_destinations, 1)
        self.assertEqual(len(state.forwarding_entries), 2)

        managed_route = next(entry for entry in state.forwarding_entries if entry["gateway"] == "192.168.178.2")
        self.assertFalse(managed_route["enabled"])
        unmanaged_route = next(entry for entry in state.forwarding_entries if entry["gateway"] == "192.168.178.9")
        self.assertTrue(unmanaged_route["enabled"])
