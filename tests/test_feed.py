from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.errors import InvalidFeedError, NetworkError  # noqa: E402
from stopliga.feed import (  # noqa: E402
    extract_hayahora_active_ips,
    load_feed_snapshot,
    load_status_snapshot,
    parse_ip_list,
    parse_status_payload,
    resolve_dns_addresses,
)
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

    def test_parse_status_payload_uses_hayahora_site_hero_heuristic(self) -> None:
        payload = """
        {
          "lastUpdate": "2026-04-21 17:44:56",
          "data": [
            {
              "ip": "104.16.93.114",
              "description": "Cloudflare",
              "isp": "Movistar",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:00:00Z", "state": false},
                {"timestamp": "2026-04-21T17:05:00Z", "state": true}
              ]
            },
            {
              "ip": "104.16.93.114",
              "description": "Cloudflare",
              "isp": "Orange",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:10:00Z", "state": false}
              ]
            }
          ]
        }
        """
        parsed, is_blocked = parse_status_payload(payload)
        self.assertFalse(is_blocked)
        self.assertEqual(parsed["source"], "hayahora-history-json")
        self.assertEqual(parsed["activeIpCount"], 1)
        self.assertEqual(parsed["confirmedIpCount"], 0)
        self.assertEqual(parsed["strategy"], "hayahora-site-hero")

    def test_parse_status_payload_marks_blocked_when_sentinel_pair_is_active(self) -> None:
        payload = """
        {
          "lastUpdate": "2026-04-21 17:44:56",
          "data": [
            {
              "ip": "188.114.96.5",
              "description": "Cloudflare",
              "isp": "Movistar",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:05:00Z", "state": true}
              ]
            },
            {
              "ip": "188.114.97.5",
              "description": "Cloudflare",
              "isp": "DIGI",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:10:00Z", "state": true}
              ]
            }
          ]
        }
        """
        parsed, is_blocked = parse_status_payload(payload)
        self.assertTrue(is_blocked)
        self.assertEqual(parsed["confirmedIpCount"], 0)
        self.assertTrue(parsed["sentinelPairBlocked"])
        self.assertEqual(parsed["sentinelPairHitSample"], ["188.114.96.5", "188.114.97.5"])

    def test_parse_status_payload_marks_blocked_when_many_cloudflare_ips_match_multiple_isps(self) -> None:
        entries: list[str] = []
        for ip_index in range(11):
            ip = f"104.16.93.{10 + ip_index}"
            for isp in ("Movistar", "Orange", "DIGI"):
                entries.append(
                    f"""
                    {{
                      "ip": "{ip}",
                      "description": "Cloudflare",
                      "isp": "{isp}",
                      "stateChanges": [{{"timestamp": "2026-04-21T17:05:00Z", "state": true}}]
                    }}
                    """
                )
        payload = """
        {
          "lastUpdate": "2026-04-21 17:44:56",
          "data": [%s]
        }
        """ % ",".join(entries)
        parsed, is_blocked = parse_status_payload(payload)
        self.assertTrue(is_blocked)
        self.assertEqual(parsed["confirmedIpCount"], 11)
        self.assertEqual(parsed["minConfirmedIpCount"], 11)

    def test_parse_status_payload_ignores_non_cloudflare_entries_for_global_state(self) -> None:
        payload = """
        {
          "lastUpdate": "2026-04-21 17:44:56",
          "data": [
            {
              "ip": "104.16.93.114",
              "description": "Backblaze",
              "isp": "Movistar",
              "stateChanges": [
                {"timestamp": "2026-04-21T17:05:00Z", "state": true}
              ]
            }
          ]
        }
        """
        parsed, is_blocked = parse_status_payload(payload)
        self.assertFalse(is_blocked)
        self.assertEqual(parsed["activeIpCount"], 0)

    def test_extract_hayahora_active_ips_can_filter_by_isp(self) -> None:
        payload = {
            "lastUpdate": "2026-04-23 09:16:40",
            "data": [
                {
                    "ip": "104.16.93.114",
                    "isp": "DIGI",
                    "stateChanges": [
                        {"timestamp": "2026-04-23 08:00:00Z", "state": False},
                        {"timestamp": "2026-04-23 09:00:00Z", "state": True},
                    ],
                },
                {
                    "ip": "104.16.93.115",
                    "isp": "Movistar",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": True}],
                },
                {
                    "ip": "104.16.93.116",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": False}],
                },
            ],
        }

        destinations, inspected, invalid = extract_hayahora_active_ips(payload, isp=" digi ")

        self.assertEqual(destinations, ["104.16.93.114"])
        self.assertEqual(inspected, 3)
        self.assertEqual(invalid, [])

    def test_extract_hayahora_active_ips_matches_isp_case_insensitively(self) -> None:
        payload = {
            "lastUpdate": "2026-04-23 09:16:40",
            "data": [
                {
                    "ip": "104.16.93.114",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": True}],
                },
            ],
        }

        destinations, _, _ = extract_hayahora_active_ips(payload, isp="digi")

        self.assertEqual(destinations, ["104.16.93.114"])

    def test_extract_hayahora_active_ips_ignores_stale_active_entries(self) -> None:
        payload = {
            "lastUpdate": "2026-04-23 09:16:40",
            "data": [
                {
                    "ip": "104.16.93.114",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-21 09:00:00Z", "state": True}],
                },
                {
                    "ip": "104.16.93.115",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": True}],
                },
            ],
        }

        destinations, _, _ = extract_hayahora_active_ips(payload, isp="DIGI")

        self.assertEqual(destinations, ["104.16.93.115"])

    def test_extract_hayahora_active_ips_rejects_unknown_isp(self) -> None:
        payload = {
            "lastUpdate": "2026-04-23 09:16:40",
            "data": [
                {
                    "ip": "104.16.93.114",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": True}],
                },
                {
                    "ip": "104.16.93.115",
                    "isp": "Movistar",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": True}],
                },
            ],
        }

        with self.assertRaisesRegex(InvalidFeedError, "Valid options: DIGI, Movistar"):
            extract_hayahora_active_ips(payload, isp="Telefonica")

    def test_extract_hayahora_active_ips_uses_all_isps_without_filter(self) -> None:
        payload = {
            "lastUpdate": "2026-04-23 09:16:40",
            "data": [
                {
                    "ip": "104.16.93.114",
                    "isp": "DIGI",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": True}],
                },
                {
                    "ip": "104.16.93.115",
                    "isp": "Movistar",
                    "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": "active"}],
                },
            ],
        }

        destinations, _, _ = extract_hayahora_active_ips(payload)

        self.assertEqual(destinations, ["104.16.93.114", "104.16.93.115"])

    def test_parse_ip_list_dedupes_and_sorts(self) -> None:
        raw = """
        # comment
        192.0.2.0/24
        2001:db8::/32
        192.0.2.42
        192.0.2.42/32
        192.0.2.42
        """
        destinations, _, invalid = parse_ip_list(raw, policy="fail")
        self.assertEqual(
            destinations,
            ["192.0.2.0/24", "192.0.2.42", "192.0.2.42/32", "2001:db8::/32"],
        )
        self.assertEqual(invalid, [])

    def test_parse_ip_list_fail_fast_on_invalid_input(self) -> None:
        with self.assertRaises(InvalidFeedError):
            parse_ip_list("not-an-ip\n", policy="fail")

    def test_parse_ip_list_can_ignore_invalid_input(self) -> None:
        destinations, _, invalid = parse_ip_list("192.0.2.1\nnot-an-ip\n", policy="ignore")
        self.assertEqual(destinations, ["192.0.2.1"])
        self.assertEqual(invalid, ["not-an-ip"])

    def test_parse_ip_list_rejects_feeds_over_destination_ceiling(self) -> None:
        raw = "\n".join(f"192.0.2.{index}" for index in range(1, 4))
        with self.assertRaisesRegex(InvalidFeedError, "safety ceiling 2"):
            parse_ip_list(raw, policy="fail", max_destinations=2)


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
    def test_default_status_url_uses_canonical_hayahora_json(self) -> None:
        self.assertEqual(Config().status_url, "https://hayahora.futbol/estado/data.json")

    def test_load_feed_snapshot_can_resolve_status_from_dns(self) -> None:
        config = Config(
            status_url="dns://blocked.dns.hayahora.futbol",
            retries=1,
        )

        with (
            patch(
                "stopliga.feed.fetch_text",
                return_value="""
                    {
                      "lastUpdate": "2026-04-23 09:16:40",
                      "data": [
                        {
                          "ip": "188.114.96.5",
                          "description": "Cloudflare",
                          "stateChanges": [
                            {"timestamp": "2026-04-23 09:00:00Z", "state": true}
                          ]
                        },
                        {
                          "ip": "188.114.97.5",
                          "description": "Cloudflare",
                          "stateChanges": [
                            {"timestamp": "2026-04-23 09:00:00Z", "state": true}
                          ]
                        }
                      ]
                    }
                    """,
            ),
            patch("stopliga.feed.resolve_dns_addresses") as dns_mock,
        ):
            snapshot = load_feed_snapshot(config)

        self.assertTrue(snapshot.is_blocked)
        self.assertEqual(snapshot.destinations, ["188.114.96.5", "188.114.97.5"])
        self.assertEqual(snapshot.raw_status["source"], "hayahora-history-json")
        dns_mock.assert_not_called()

    def test_load_feed_snapshot_treats_empty_dns_status_as_not_blocked(self) -> None:
        config = Config(
            status_url="dns://blocked.dns.hayahora.futbol",
            retries=1,
        )

        with (
            patch(
                "stopliga.feed.fetch_text",
                return_value="""
                    {
                      "lastUpdate": "2026-04-23 09:16:40",
                      "data": []
                    }
                    """,
            ),
            patch("stopliga.feed.resolve_dns_addresses") as dns_mock,
        ):
            snapshot = load_feed_snapshot(config)

        self.assertFalse(snapshot.is_blocked)
        self.assertEqual(snapshot.destinations, [])
        self.assertEqual(snapshot.raw_status["source"], "hayahora-history-json")
        self.assertEqual(snapshot.raw_status["activeIpCount"], 0)
        dns_mock.assert_not_called()

    def test_load_feed_snapshot_does_not_fall_back_when_hayahora_json_fails(self) -> None:
        config = Config(
            status_url="dns://blocked.dns.hayahora.futbol",
            retries=1,
        )

        def fake_fetch(url: str, **_: object) -> str:
            if url == "https://hayahora.futbol/estado/data.json":
                raise NetworkError("hayahora json unavailable")
            raise AssertionError(f"unexpected url: {url}")

        with (
            patch("stopliga.feed.fetch_text", side_effect=fake_fetch),
            patch("stopliga.feed.resolve_dns_addresses", return_value=["104.21.0.97"]) as dns_mock,
        ):
            with self.assertRaises(NetworkError):
                load_feed_snapshot(config)

        dns_mock.assert_not_called()

    def test_load_status_snapshot_uses_larger_limit_for_canonical_hayahora_json(self) -> None:
        config = Config(
            status_url="https://hayahora.futbol/estado/data.json",
            max_response_bytes=1024,
            retries=1,
        )
        fetch_calls: list[dict[str, object]] = []

        def fake_fetch(url: str, **kwargs: object) -> str:
            fetch_calls.append({"url": url, **kwargs})
            return """
            {
              "lastUpdate": "2026-04-23 09:16:40",
              "data": []
            }
            """

        with patch("stopliga.feed.fetch_text", side_effect=fake_fetch):
            raw_status, is_blocked = load_status_snapshot(config)

        self.assertFalse(is_blocked)
        self.assertEqual(raw_status["source"], "hayahora-history-json")
        self.assertEqual(len(fetch_calls), 1)
        self.assertEqual(fetch_calls[0]["url"], "https://hayahora.futbol/estado/data.json")
        self.assertEqual(fetch_calls[0]["max_bytes"], 16 * 1024 * 1024)

    def test_load_feed_snapshot_can_use_hayahora_active_isp_destinations(self) -> None:
        config = Config(
            hayahora_isp="DIGI",
            retries=1,
        )

        with patch(
            "stopliga.feed.fetch_text",
            return_value="""
            {
              "lastUpdate": "2026-04-23 09:16:40",
              "data": [
                {
                  "ip": "188.114.96.5",
                  "description": "Cloudflare",
                  "isp": "DIGI",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                },
                {
                  "ip": "104.16.93.114",
                  "description": "Cloudflare",
                  "isp": "Movistar",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                }
              ]
            }
            """,
        ):
            snapshot = load_feed_snapshot(config)

        self.assertFalse(snapshot.is_blocked)
        self.assertFalse(snapshot.desired_enabled)
        self.assertEqual(snapshot.destinations, ["188.114.96.5"])
        self.assertEqual(snapshot.raw_line_count, 2)

    def test_load_feed_snapshot_uses_hayahora_site_state_not_destination_presence(self) -> None:
        config = Config(retries=1)

        with patch(
            "stopliga.feed.fetch_text",
            return_value="""
            {
              "lastUpdate": "2026-04-23 09:16:40",
              "data": [
                {
                  "ip": "104.16.93.114",
                  "description": "Cloudflare",
                  "isp": "DIGI",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                }
              ]
            }
            """,
        ):
            snapshot = load_feed_snapshot(config)

        self.assertFalse(snapshot.is_blocked)
        self.assertFalse(snapshot.desired_enabled)
        self.assertEqual(snapshot.destinations, ["104.16.93.114"])
        self.assertEqual(snapshot.raw_status["strategy"], "hayahora-site-hero")

    def test_load_feed_snapshot_decodes_structured_hayahora_json_once(self) -> None:
        config = Config(retries=1)
        payload = """
            {
              "lastUpdate": "2026-04-23 09:16:40",
              "data": [
                {
                  "ip": "188.114.96.5",
                  "description": "Cloudflare",
                  "isp": "DIGI",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                }
              ]
            }
            """
        real_json_loads = json.loads
        loads_calls = 0

        def counting_json_loads(raw: str) -> object:
            nonlocal loads_calls
            loads_calls += 1
            return real_json_loads(raw)

        with (
            patch("stopliga.feed.fetch_text", return_value=payload),
            patch("stopliga.feed.json.loads", side_effect=counting_json_loads),
        ):
            snapshot = load_feed_snapshot(config)

        self.assertEqual(loads_calls, 1)
        self.assertEqual(snapshot.destinations, ["188.114.96.5"])

    def test_load_feed_snapshot_handles_blocked_then_unblocked_payloads(self) -> None:
        config = Config(retries=1)
        blocked_payload = """
            {
              "lastUpdate": "2026-04-23 09:16:40",
              "data": [
                {
                  "ip": "188.114.96.5",
                  "description": "Cloudflare",
                  "isp": "DIGI",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                },
                {
                  "ip": "188.114.97.5",
                  "description": "Cloudflare",
                  "isp": "Movistar",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                }
              ]
            }
            """
        unblocked_payload = """
            {
              "lastUpdate": "2026-04-23 09:21:40",
              "data": [
                {
                  "ip": "188.114.96.5",
                  "description": "Cloudflare",
                  "isp": "DIGI",
                  "stateChanges": [
                    {"timestamp": "2026-04-23 09:00:00Z", "state": true},
                    {"timestamp": "2026-04-23 09:20:00Z", "state": false}
                  ]
                }
              ]
            }
            """

        with patch("stopliga.feed.fetch_text", side_effect=[blocked_payload, unblocked_payload]):
            blocked_snapshot = load_feed_snapshot(config)
            unblocked_snapshot = load_feed_snapshot(config)

        self.assertTrue(blocked_snapshot.is_blocked)
        self.assertTrue(blocked_snapshot.desired_enabled)
        self.assertEqual(blocked_snapshot.destinations, ["188.114.96.5", "188.114.97.5"])
        self.assertFalse(unblocked_snapshot.is_blocked)
        self.assertFalse(unblocked_snapshot.desired_enabled)
        self.assertEqual(unblocked_snapshot.destinations, [])

    def test_load_feed_snapshot_active_mode_uses_all_isps_when_filter_is_absent(self) -> None:
        config = Config(retries=1)

        with patch(
            "stopliga.feed.fetch_text",
            return_value="""
            {
              "lastUpdate": "2026-04-23 09:16:40",
              "data": [
                {
                  "ip": "188.114.96.5",
                  "description": "Cloudflare",
                  "isp": "DIGI",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                },
                {
                  "ip": "188.114.97.5",
                  "description": "Cloudflare",
                  "isp": "Movistar",
                  "stateChanges": [{"timestamp": "2026-04-23 09:00:00Z", "state": true}]
                }
              ]
            }
            """,
        ):
            snapshot = load_feed_snapshot(config)

        self.assertTrue(snapshot.desired_enabled)
        self.assertEqual(snapshot.destinations, ["188.114.96.5", "188.114.97.5"])

    def test_load_status_snapshot_uses_canonical_hayahora_json_for_dns_alias(self) -> None:
        config = Config(
            status_url="dns://blocked.dns.hayahora.futbol",
            retries=1,
        )

        with (
            patch(
                "stopliga.feed.fetch_text",
                return_value="""
                {
                  "lastUpdate": "2026-04-23 09:16:40",
                  "data": []
                }
                """,
            ),
            patch("stopliga.feed.resolve_dns_addresses") as dns_mock,
        ):
            raw_status, is_blocked = load_status_snapshot(config)

        self.assertFalse(is_blocked)
        self.assertEqual(raw_status["source"], "hayahora-history-json")
        dns_mock.assert_not_called()

    def test_resolve_dns_addresses_returns_empty_list_when_dns_has_no_records(self) -> None:
        no_record_errno = getattr(socket, "EAI_NONAME", 8)
        with patch("stopliga.feed.socket.getaddrinfo", side_effect=socket.gaierror(no_record_errno, "no records")):
            self.assertEqual(resolve_dns_addresses("blocked.dns.hayahora.futbol", retries=1), [])


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
