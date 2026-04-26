from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.utils import sort_ip_tokens  # noqa: E402


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
