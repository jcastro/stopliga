from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.config import build_parser, load_config  # noqa: E402
from stopliga.errors import ConfigError  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_cli_overrides_environment_and_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "stopliga.toml"
            config_path.write_text(
                """
[app]
route_name = "FromFile"
run_mode = "once"

[unifi]
host = "10.0.0.1"
username = "file-user"
password = "file-pass"
site = "default"
                """.strip(),
                encoding="utf-8",
            )
            parser = build_parser()
            args = parser.parse_args(["--config", str(config_path), "--route-name", "FromCLI"])
            config = load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_USERNAME": "env-user",
                    "UNIFI_PASSWORD": "env-pass",
                },
            )
            self.assertEqual(config.route_name, "FromCLI")
            self.assertEqual(config.host, "10.0.0.2")
            self.assertEqual(config.username, "env-user")

    def test_healthcheck_config_can_load_without_unifi_credentials(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--healthcheck"])
        config = load_config(args, {}, validate=False)
        self.assertEqual(config.run_mode, "once")

    def test_partial_bootstrap_configuration_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_USERNAME": "env-user",
                    "UNIFI_PASSWORD": "env-pass",
                    "STOPLIGA_VPN_NAME": "Mullvad DE",
                },
            )
