from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.errors import InvalidFeedError  # noqa: E402
from stopliga.feed import parse_ip_list, parse_status_payload  # noqa: E402
from stopliga.state import StateStore  # noqa: E402
from stopliga.unifi import build_ip_objects, build_route_update_template  # noqa: E402


class FeedParsingTests(unittest.TestCase):
    def test_parse_status_payload_supports_all_documented_keys(self) -> None:
        _, value_a = parse_status_payload('{"isBlocked": true}')
        _, value_b = parse_status_payload('{"blocked": false}')
        _, value_c = parse_status_payload('{"state": "blocked"}')
        self.assertTrue(value_a)
        self.assertFalse(value_b)
        self.assertTrue(value_c)

    def test_parse_ip_list_dedupes_and_sorts(self) -> None:
        raw = """
        # comment
        192.0.2.0/24
        2001:db8::/32
        192.0.2.42
        192.0.2.42
        """
        destinations, _, invalid = parse_ip_list(raw, policy="fail")
        self.assertEqual(
            destinations,
            ["192.0.2.0/24", "192.0.2.42", "2001:db8::/32"],
        )
        self.assertEqual(invalid, [])

    def test_parse_ip_list_fail_fast_on_invalid_input(self) -> None:
        with self.assertRaises(InvalidFeedError):
            parse_ip_list("not-an-ip\n", policy="fail")

    def test_parse_ip_list_can_ignore_invalid_input(self) -> None:
        destinations, _, invalid = parse_ip_list("192.0.2.1\nnot-an-ip\n", policy="ignore")
        self.assertEqual(destinations, ["192.0.2.1"])
        self.assertEqual(invalid, ["not-an-ip"])


class StateStoreTests(unittest.TestCase):
    def test_healthcheck_uses_last_success_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.json")
            recent = datetime.now(timezone.utc) - timedelta(seconds=10)
            store.path.write_text(
                f"""{{
                  "status": "success",
                  "last_success_at": "{recent.isoformat()}"
                }}""",
                encoding="utf-8",
            )
            healthy, message = store.healthcheck(60)
            self.assertTrue(healthy)
            self.assertIn("healthy", message)

    def test_healthcheck_rejects_future_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.json")
            future = datetime.now(timezone.utc) + timedelta(hours=1)
            store.path.write_text(
                f"""{{
                  "status": "success",
                  "last_success_at": "{future.isoformat()}"
                }}""",
                encoding="utf-8",
            )
            healthy, message = store.healthcheck(60)
            self.assertFalse(healthy)
            self.assertIn("future", message)


class RoutePayloadTests(unittest.TestCase):
    def test_build_ip_objects_preserves_version_field_for_mixed_ipv4_ipv6(self) -> None:
        built = build_ip_objects(
            ["192.0.2.1", "2001:db8::/32"],
            [
                {"ip_or_subnet": "192.0.2.2", "ip_version": "v4", "ports": [], "port_ranges": []},
                {"ip_or_subnet": "2001:db8::1", "ip_version": "v6", "ports": [], "port_ranges": []},
            ],
        )
        self.assertEqual(
            built,
            [
                {"ip_or_subnet": "192.0.2.1", "ip_version": "v4", "ports": [], "port_ranges": []},
                {"ip_or_subnet": "2001:db8::/32", "ip_version": "v6", "ports": [], "port_ranges": []},
            ],
        )

    def test_build_route_update_template_discards_unknown_fields(self) -> None:
        payload = build_route_update_template(
            {
                "_id": "route-1",
                "description": "LaLiga",
                "enabled": True,
                "network_id": "vpn-network-1",
                "target_devices": [],
                "ip_addresses": [{"ip_or_subnet": "192.0.2.1", "ip_version": "v4"}],
                "computed_status": "internal-only",
                "destination": {
                    "items": ["192.0.2.1"],
                    "temporary_state": "drop-me",
                },
            }
        )
        self.assertNotIn("_id", payload)
        self.assertNotIn("computed_status", payload)
        self.assertEqual(payload["destination"], {"items": ["192.0.2.1"]})
