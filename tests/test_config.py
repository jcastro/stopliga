from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

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
api_key = "file-api-key"
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
                    "UNIFI_API_KEY": "env-api-key",
                },
            )
            self.assertEqual(config.route_name, "FromCLI")
            self.assertEqual(config.host, "10.0.0.2")
            self.assertEqual(config.api_key, "env-api-key")

    def test_healthcheck_config_can_load_without_unifi_credentials(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--healthcheck"])
        config = load_config(args, {}, validate=False)
        self.assertEqual(config.run_mode, "once")

    def test_api_key_is_loaded_for_local_mode(self) -> None:
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

    def test_local_mode_requires_api_key(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                },
            )

    def test_partial_bootstrap_configuration_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "env-api-key",
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

    def test_telegram_group_and_chat_id_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_TELEGRAM_BOT_TOKEN": "123456:test",
                    "STOPLIGA_TELEGRAM_CHAT_ID": "1234",
                    "STOPLIGA_TELEGRAM_GROUP_ID": "-1001234567890",
                },
            )

    def test_telegram_topic_requires_chat_target(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_TELEGRAM_TOPIC_ID": "42",
                },
            )

    def test_telegram_topic_requires_group_target(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_TELEGRAM_BOT_TOKEN": "123456:test",
                    "STOPLIGA_TELEGRAM_CHAT_ID": "1234",
                    "STOPLIGA_TELEGRAM_TOPIC_ID": "42",
                },
            )

    def test_telegram_group_and_topic_load_successfully(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.0.2",
                "UNIFI_API_KEY": "test-api-key",
                "STOPLIGA_TELEGRAM_BOT_TOKEN": "123456:test",
                "STOPLIGA_TELEGRAM_GROUP_ID": "-1001234567890",
                "STOPLIGA_TELEGRAM_TOPIC_ID": "42",
            },
        )
        self.assertEqual(config.telegram_group_id, "-1001234567890")
        self.assertEqual(config.telegram_topic_id, 42)

    def test_empty_route_name_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_ROUTE_NAME": "   ",
                },
            )

    def test_gotify_url_with_embedded_credentials_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_GOTIFY_URL": "https://user:pass@gotify.example",
                    "STOPLIGA_GOTIFY_TOKEN": "token",
                },
            )

    def test_notification_retries_must_be_positive(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_NOTIFICATION_RETRIES": "0",
                },
            )

    def test_invalid_unifi_host_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "https://10.0.0.2/controller",
                    "UNIFI_API_KEY": "test-api-key",
                },
            )

    def test_ipv6_unifi_host_is_accepted(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "2001:db8::1",
                "UNIFI_API_KEY": "test-api-key",
            },
        )
        self.assertEqual(config.host, "2001:db8::1")

    def test_gotify_plain_http_requires_explicit_override(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_GOTIFY_URL": "http://gotify.example",
                    "STOPLIGA_GOTIFY_TOKEN": "token",
                },
            )

    def test_telegram_tls_cannot_be_disabled(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_TELEGRAM_BOT_TOKEN": "123456:test",
                    "STOPLIGA_TELEGRAM_CHAT_ID": "1234",
                    "STOPLIGA_TELEGRAM_VERIFY_TLS": "false",
                },
            )

    def test_max_response_bytes_must_have_safe_floor(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_MAX_RESPONSE_BYTES": "512",
                },
            )

    def test_webui_config_defaults_to_disabled(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {"UNIFI_HOST": "10.0.0.1", "UNIFI_API_KEY": "test-key"},
        )
        self.assertFalse(config.webui_enabled)
        self.assertEqual(config.webui_port, 8080)
        self.assertEqual(config.webui_host, "0.0.0.0")

    def test_webui_config_loads_from_env(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.0.1",
                "UNIFI_API_KEY": "test-key",
                "STOPLIGA_WEBUI_ENABLED": "true",
                "STOPLIGA_WEBUI_PORT": "9090",
                "STOPLIGA_WEBUI_HOST": "127.0.0.1",
            },
        )
        self.assertTrue(config.webui_enabled)
        self.assertEqual(config.webui_port, 9090)
        self.assertEqual(config.webui_host, "127.0.0.1")
