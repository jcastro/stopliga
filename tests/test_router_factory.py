from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.errors import ConfigError  # noqa: E402
from stopliga.models import Config  # noqa: E402
from stopliga.routers.factory import create_router_driver  # noqa: E402
from stopliga.routers.fritzbox import FRITZBoxRouterDriver  # noqa: E402
from stopliga.routers.omada import OmadaRouterDriver  # noqa: E402
from stopliga.routers.opnsense import OPNsenseRouterDriver  # noqa: E402
from stopliga.routers.unifi import UniFiRouterDriver  # noqa: E402


class RouterFactoryTests(unittest.TestCase):
    def test_factory_creates_unifi_driver(self) -> None:
        driver = create_router_driver(Config())
        self.assertIsInstance(driver, UniFiRouterDriver)

    def test_factory_creates_omada_driver(self) -> None:
        driver = create_router_driver(Config(router_type="omada"))  # type: ignore[arg-type]
        self.assertIsInstance(driver, OmadaRouterDriver)

    def test_factory_creates_fritzbox_driver(self) -> None:
        driver = create_router_driver(Config(router_type="fritzbox"))  # type: ignore[arg-type]
        self.assertIsInstance(driver, FRITZBoxRouterDriver)

    def test_factory_creates_opnsense_driver(self) -> None:
        driver = create_router_driver(Config(router_type="opnsense"))  # type: ignore[arg-type]
        self.assertIsInstance(driver, OPNsenseRouterDriver)

    def test_factory_rejects_unknown_router_type(self) -> None:
        with self.assertRaises(ConfigError):
            create_router_driver(Config(router_type="openwrt"))  # type: ignore[arg-type]
