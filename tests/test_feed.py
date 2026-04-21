from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.errors import InvalidFeedError  # noqa: E402
from stopliga.feed import load_feed_snapshot, parse_ip_list, parse_status_payload  # noqa: E402
from stopliga.models import Config  # noqa: E402
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

    def test_parse_status_payload_supports_hayahora_history_json(self) -> None:
        payload = """
        {
          "lastUpdate": "2026-04-21 17:44:56",
          "data": [
            {
              "ip": "104.16.93.114",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:00:00Z", "state": false},
                {"timestamp": "2026-04-21T17:05:00Z", "state": true}
              ]
            },
            {
              "ip": "104.16.93.114",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:10:00Z", "state": false}
              ]
            }
          ]
        }
        """
        parsed, is_blocked = parse_status_payload(payload)
        self.assertTrue(is_blocked)
        self.assertEqual(parsed["source"], "hayahora-history-json")
        self.assertEqual(parsed["activeIpCount"], 1)

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

    def test_healthcheck_rejects_recent_error_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.json")
            recent = datetime.now(timezone.utc) - timedelta(seconds=10)
            store.path.write_text(
                f"""{{
                  "status": "error",
                  "consecutive_failures": 3,
                  "last_success_at": "{recent.isoformat()}"
                }}""",
                encoding="utf-8",
            )
            healthy, message = store.healthcheck(60)
            self.assertFalse(healthy)
            self.assertIn("consecutive_failures=3", message)

    def test_healthcheck_rejects_reconciliation_required_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.json")
            recent = datetime.now(timezone.utc) - timedelta(seconds=10)
            store.path.write_text(
                f"""{{
                  "status": "success",
                  "reconciliation_required": true,
                  "last_success_at": "{recent.isoformat()}"
                }}""",
                encoding="utf-8",
            )
            healthy, message = store.healthcheck(60)
            self.assertFalse(healthy)
            self.assertIn("reconciliation", message)


class FeedLoadingTests(unittest.TestCase):
    def test_load_feed_snapshot_pins_github_raw_urls_to_single_revision(self) -> None:
        config = Config(
            status_url="https://raw.githubusercontent.com/example/repo/main/status.json",
            ip_list_url="https://raw.githubusercontent.com/example/repo/main/ip_list.txt",
            retries=1,
        )
        calls: list[str] = []
        responses = {
            "https://api.github.com/repos/example/repo/commits/main": '{"sha": "deadbeef"}',
            "https://raw.githubusercontent.com/example/repo/deadbeef/status.json": '{"isBlocked": true}',
            "https://raw.githubusercontent.com/example/repo/deadbeef/ip_list.txt": "192.0.2.1\n",
        }

        def fake_fetch(url: str, **_: object) -> str:
            calls.append(url)
            return responses[url]

        with patch("stopliga.feed.fetch_text", side_effect=fake_fetch):
            snapshot = load_feed_snapshot(config)

        self.assertTrue(snapshot.is_blocked)
        self.assertEqual(snapshot.destinations, ["192.0.2.1"])
        self.assertEqual(
            calls,
            [
                "https://api.github.com/repos/example/repo/commits/main",
                "https://raw.githubusercontent.com/example/repo/deadbeef/status.json",
                "https://raw.githubusercontent.com/example/repo/deadbeef/ip_list.txt",
            ],
        )

    def test_load_feed_snapshot_can_degrade_when_revision_resolution_fails(self) -> None:
        config = Config(
            status_url="https://raw.githubusercontent.com/example/repo/main/status.json",
            ip_list_url="https://raw.githubusercontent.com/example/repo/main/ip_list.txt",
            retries=1,
            strict_feed_consistency=False,
        )
        calls: list[str] = []
        responses = {
            "https://raw.githubusercontent.com/example/repo/main/status.json": '{"isBlocked": false}',
            "https://raw.githubusercontent.com/example/repo/main/ip_list.txt": "192.0.2.1\n",
        }

        def fake_fetch(url: str, **_: object) -> str:
            calls.append(url)
            if url.startswith("https://api.github.com/"):
                raise InvalidFeedError("github api unavailable")
            return responses[url]

        with patch("stopliga.feed.fetch_text", side_effect=fake_fetch):
            snapshot = load_feed_snapshot(config)

        self.assertFalse(snapshot.is_blocked)
        self.assertEqual(snapshot.destinations, ["192.0.2.1"])
        self.assertEqual(
            calls,
            [
                "https://api.github.com/repos/example/repo/commits/main",
                "https://raw.githubusercontent.com/example/repo/main/status.json",
                "https://raw.githubusercontent.com/example/repo/main/ip_list.txt",
            ],
        )

    def test_load_feed_snapshot_can_resolve_status_from_dns(self) -> None:
        config = Config(
            status_url="dns://blocked.dns.hayahora.futbol",
            ip_list_url="https://raw.githubusercontent.com/example/repo/main/ip_list.txt",
            retries=1,
        )

        with (
            patch("stopliga.feed.resolve_dns_addresses", return_value=["104.21.0.97", "172.67.205.181"]) as dns_mock,
            patch("stopliga.feed.fetch_text", return_value="192.0.2.1\n"),
        ):
            snapshot = load_feed_snapshot(config)

        self.assertTrue(snapshot.is_blocked)
        self.assertEqual(snapshot.destinations, ["192.0.2.1"])
        self.assertEqual(snapshot.raw_status["source"], "dns")
        dns_mock.assert_called_once_with("blocked.dns.hayahora.futbol", retries=1)


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
