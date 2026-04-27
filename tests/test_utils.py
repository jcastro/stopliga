from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.models import Config, SiteContext  # noqa: E402
from stopliga.routers.unifi import V2TrafficRoutesBackend  # noqa: E402
from stopliga.utils import sort_canonical_ip_tokens, sort_ip_tokens  # noqa: E402


class IpTokenSortingTests(unittest.TestCase):
    def test_sort_ip_tokens_canonicalizes_dedupes_and_orders_hosts_before_matching_cidrs(self) -> None:
        values = [
            "2001:db8::/32",
            "192.0.2.42/32",
            "192.0.2.42",
            "192.0.2.42",
            "192.0.2.0/24",
        ]

        self.assertEqual(
            sort_ip_tokens(values),
            ["192.0.2.0/24", "192.0.2.42", "192.0.2.42/32", "2001:db8::/32"],
        )

    def test_sort_canonical_ip_tokens_dedupes_without_recanonicalizing_inputs(self) -> None:
        self.assertEqual(
            sort_canonical_ip_tokens(["192.0.2.42", "192.0.2.0/24", "192.0.2.42"]),
            ["192.0.2.0/24", "192.0.2.42"],
        )


class _DummyUniFiClient:
    config = Config(destination_field="ip_addresses")

    def discover_network_prefix(self) -> str:
        return ""


class UniFiPlanningFastPathTests(unittest.TestCase):
    def test_noop_plan_does_not_keep_rollback_copy(self) -> None:
        desired = ["192.0.2.10", "198.51.100.0/24"]
        route = {
            "_id": "route-1",
            "enabled": True,
            "description": "StopLiga",
            "matching_target": "IP",
            "ip_addresses": [{"ip_or_subnet": token, "ip_version": "v4"} for token in desired],
        }
        backend = V2TrafficRoutesBackend(_DummyUniFiClient(), SiteContext(internal_name="default"))

        plan = backend.build_plan("/routes", route, desired, True)

        self.assertFalse(plan.has_changes)
        self.assertIsNone(plan.route_payload)
        self.assertIsNone(plan.raw_route)
