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

    def test_api_key_can_replace_username_and_password(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.0.2",
                "UNIFI_API_KEY": "test-api-key",
            },
        )
        self.assertEqual(config.host, "10.0.0.2")
        self.assertEqual(config.api_key, "test-api-key")
        self.assertIsNone(config.username)
        self.assertIsNone(config.password)

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

    def test_private_feed_hosts_are_rejected_by_default(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_STATUS_URL": "https://10.0.0.3/feed/status.json",
                    "STOPLIGA_IP_LIST_URL": "https://10.0.0.3/feed/ip_list.txt",
                },
            )

    def test_loopback_http_feed_urls_are_allowed(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.0.2",
                "UNIFI_API_KEY": "test-api-key",
                "STOPLIGA_STATUS_URL": "http://127.0.0.1/status.json",
                "STOPLIGA_IP_LIST_URL": "http://localhost/ip_list.txt",
            },
        )
        self.assertEqual(config.status_url, "http://127.0.0.1/status.json")
        self.assertEqual(config.ip_list_url, "http://localhost/ip_list.txt")

    def test_secret_files_can_provide_unifi_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            api_key_file = Path(tmpdir) / "api_key"
            password_file = Path(tmpdir) / "password"
            api_key_file.write_text("secret-api-key\n", encoding="utf-8")
            password_file.write_text("secret-password\n", encoding="utf-8")
            parser = build_parser()
            args = parser.parse_args([])
            config = load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_USERNAME": "env-user",
                    "UNIFI_API_KEY_FILE": str(api_key_file),
                    "UNIFI_PASSWORD_FILE": str(password_file),
                },
            )
            self.assertEqual(config.api_key, "secret-api-key")
            self.assertEqual(config.username, "env-user")
            self.assertEqual(config.password, "secret-password")

    def test_direct_secret_and_secret_file_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            api_key_file = Path(tmpdir) / "api_key"
            api_key_file.write_text("secret-api-key\n", encoding="utf-8")
            parser = build_parser()
            args = parser.parse_args([])
            with self.assertRaises(ConfigError):
                load_config(
                    args,
                    {
                        "UNIFI_HOST": "10.0.0.2",
                        "UNIFI_API_KEY": "env-api-key",
                        "UNIFI_API_KEY_FILE": str(api_key_file),
                    },
                )

    def test_partial_gotify_configuration_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_GOTIFY_URL": "https://gotify.example",
                },
            )

    def test_partial_telegram_configuration_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_TELEGRAM_CHAT_ID": "1234",
                },
            )
