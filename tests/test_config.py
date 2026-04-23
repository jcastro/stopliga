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

    def test_invalid_log_level_from_environment_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_LOG_LEVEL": "trace",
                },
            )

    def test_boolean_value_is_not_accepted_for_integer_settings(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                    "STOPLIGA_RETRIES": "true",
                },
            )

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

    def test_router_type_defaults_to_unifi(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.0.2",
                "UNIFI_API_KEY": "test-api-key",
            },
        )
        self.assertEqual(config.router_type, "unifi")

    def test_generic_backend_alias_selects_router_type(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "STOPLIGA_BACKEND": "unifi",
                "STOPLIGA_CONTROLLER_HOST": "10.0.0.2",
                "UNIFI_API_KEY": "test-api-key",
            },
        )
        self.assertEqual(config.router_type, "unifi")
        self.assertEqual(config.host, "10.0.0.2")

    def test_current_production_style_unifi_env_still_loads(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.1.1",
                "UNIFI_API_KEY": "test-api-key",
                "UNIFI_SITE": "default",
                "UNIFI_VERIFY_TLS": "false",
                "STOPLIGA_RUN_MODE": "loop",
                "STOPLIGA_SYNC_INTERVAL_SECONDS": "300",
                "STOPLIGA_ROUTE_NAME": "StopLiga",
                "STOPLIGA_TELEGRAM_BOT_TOKEN": "123456:test-token",
                "STOPLIGA_TELEGRAM_CHAT_ID": "2165833",
            },
        )
        self.assertEqual(config.router_type, "unifi")
        self.assertEqual(config.host, "10.0.1.1")
        self.assertEqual(config.api_key, "test-api-key")
        self.assertEqual(config.site, "default")
        self.assertFalse(config.unifi_verify_tls)
        self.assertEqual(config.run_mode, "loop")
        self.assertEqual(config.interval_seconds, 300)
        self.assertEqual(config.route_name, "StopLiga")
        self.assertEqual(config.telegram_bot_token, "123456:test-token")
        self.assertEqual(config.telegram_chat_id, "2165833")

    def test_generic_controller_env_is_preferred_over_legacy_unifi_aliases(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "STOPLIGA_CONTROLLER_HOST": "10.0.0.9",
                "STOPLIGA_SITE": "lab",
                "STOPLIGA_CONTROLLER_VERIFY_TLS": "false",
                "UNIFI_HOST": "10.0.0.2",
                "UNIFI_SITE": "default",
                "UNIFI_VERIFY_TLS": "true",
                "UNIFI_API_KEY": "test-api-key",
            },
        )
        self.assertEqual(config.host, "10.0.0.9")
        self.assertEqual(config.site, "lab")
        self.assertFalse(config.unifi_verify_tls)

    def test_toml_controller_section_is_used_for_shared_connection_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "stopliga.toml"
            config_path.write_text(
                """
[app]
backend = "unifi"

[controller]
host = "10.0.0.8"
port = 8443
site = "lab"
verify_tls = false

[unifi]
api_key = "file-api-key"
                """.strip(),
                encoding="utf-8",
            )
            parser = build_parser()
            args = parser.parse_args(["--config", str(config_path)])
            config = load_config(args, {})
            self.assertEqual(config.host, "10.0.0.8")
            self.assertEqual(config.port, 8443)
            self.assertEqual(config.site, "lab")
            self.assertFalse(config.unifi_verify_tls)
            self.assertEqual(config.api_key, "file-api-key")

    def test_secret_file_env_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secret_file = Path(tmpdir) / "unifi_api_key.txt"
            secret_file.write_text("file-secret\n", encoding="utf-8")
            parser = build_parser()
            args = parser.parse_args([])
            config = load_config(
                args,
                {
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY_FILE": str(secret_file),
                },
            )
            self.assertEqual(config.api_key, "file-secret")

    def test_secret_file_must_be_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parser = build_parser()
            args = parser.parse_args([])
            with self.assertRaises(ConfigError):
                load_config(
                    args,
                    {
                        "UNIFI_HOST": "10.0.0.2",
                        "UNIFI_API_KEY": "test-api-key",
                        "STOPLIGA_GOTIFY_URL": "https://gotify.example",
                        "STOPLIGA_GOTIFY_TOKEN_FILE": tmpdir,
                    },
                )

    def test_invalid_router_type_is_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "STOPLIGA_ROUTER_TYPE": "openwrt",
                    "UNIFI_HOST": "10.0.0.2",
                    "UNIFI_API_KEY": "test-api-key",
                },
            )

    def test_omada_mode_loads_required_settings(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "STOPLIGA_ROUTER_TYPE": "omada",
                "STOPLIGA_OMADA_BASE_URL": "https://controller.example/openapi",
                "STOPLIGA_OMADA_CLIENT_ID": "client-id",
                "STOPLIGA_OMADA_CLIENT_SECRET": "client-secret",
                "STOPLIGA_OMADA_OMADAC_ID": "omadac-id",
                "STOPLIGA_OMADA_SITE": "Madrid",
                "STOPLIGA_OMADA_TARGET_TYPE": "vpn",
                "STOPLIGA_OMADA_TARGET": "WG-Madrid",
                "STOPLIGA_OMADA_SOURCE_NETWORKS": "LAN,IoT",
            },
        )
        self.assertEqual(config.router_type, "omada")
        self.assertEqual(config.omada_base_url, "https://controller.example")
        self.assertEqual(config.site, "Madrid")
        self.assertEqual(config.omada_target_type, "vpn")
        self.assertEqual(config.omada_target, "WG-Madrid")
        self.assertEqual(config.omada_source_networks, ("LAN", "IoT"))

    def test_omada_mode_can_derive_base_url_from_generic_controller_settings(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "STOPLIGA_BACKEND": "omada",
                "STOPLIGA_CONTROLLER_HOST": "controller.example",
                "STOPLIGA_CONTROLLER_PORT": "8043",
                "STOPLIGA_SITE": "Madrid",
                "STOPLIGA_CONTROLLER_VERIFY_TLS": "false",
                "OMADA_CLIENT_ID": "client-id",
                "OMADA_CLIENT_SECRET": "client-secret",
                "OMADA_CONTROLLER_ID": "omadac-id",
                "OMADA_TARGET_TYPE": "vpn",
                "OMADA_TARGET": "WG-Madrid",
            },
        )
        self.assertEqual(config.router_type, "omada")
        self.assertEqual(config.omada_base_url, "https://controller.example:8043")
        self.assertEqual(config.site, "Madrid")
        self.assertFalse(config.omada_verify_tls)
        self.assertEqual(config.omada_omadac_id, "omadac-id")

    def test_omada_mode_requires_target_type(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "STOPLIGA_ROUTER_TYPE": "omada",
                    "STOPLIGA_OMADA_BASE_URL": "https://controller.example",
                    "STOPLIGA_OMADA_CLIENT_ID": "client-id",
                    "STOPLIGA_OMADA_CLIENT_SECRET": "client-secret",
                    "STOPLIGA_OMADA_OMADAC_ID": "omadac-id",
                    "STOPLIGA_OMADA_TARGET": "WAN1",
                },
            )

    def test_keenetic_mode_loads_required_settings(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "STOPLIGA_ROUTER_TYPE": "keenetic",
                "STOPLIGA_KEENETIC_BASE_URL": "http://router.keenetic.local",
                "KEENETIC_USERNAME": "admin",
                "KEENETIC_PASSWORD": "secret",
                "KEENETIC_INTERFACE": "Wireguard1",
                "KEENETIC_GATEWAY": "10.10.10.1",
                "KEENETIC_AUTO": "true",
                "KEENETIC_REJECT": "true",
            },
        )
        self.assertEqual(config.router_type, "keenetic")
        self.assertEqual(config.keenetic_base_url, "http://router.keenetic.local")
        self.assertEqual(config.keenetic_username, "admin")
        self.assertEqual(config.keenetic_password, "secret")
        self.assertEqual(config.keenetic_interface, "Wireguard1")
        self.assertEqual(config.keenetic_gateway, "10.10.10.1")
        self.assertTrue(config.keenetic_auto)
        self.assertTrue(config.keenetic_reject)

    def test_keenetic_reject_requires_auto(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "STOPLIGA_ROUTER_TYPE": "keenetic",
                    "STOPLIGA_KEENETIC_BASE_URL": "http://router.keenetic.local",
                    "KEENETIC_USERNAME": "admin",
                    "KEENETIC_PASSWORD": "secret",
                    "KEENETIC_INTERFACE": "Wireguard1",
                    "KEENETIC_AUTO": "false",
                    "KEENETIC_REJECT": "true",
                },
            )

    def test_omada_route_name_length_is_validated(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with self.assertRaises(ConfigError):
            load_config(
                args,
                {
                    "STOPLIGA_ROUTER_TYPE": "omada",
                    "STOPLIGA_OMADA_BASE_URL": "https://controller.example",
                    "STOPLIGA_OMADA_CLIENT_ID": "client-id",
                    "STOPLIGA_OMADA_CLIENT_SECRET": "client-secret",
                    "STOPLIGA_OMADA_OMADAC_ID": "omadac-id",
                    "STOPLIGA_OMADA_TARGET_TYPE": "vpn",
                    "STOPLIGA_OMADA_TARGET": "WG-Madrid",
                    "STOPLIGA_ROUTE_NAME": "X" * 65,
                },
            )

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

    def test_dns_status_feed_urls_are_allowed(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "UNIFI_HOST": "10.0.0.2",
                "UNIFI_API_KEY": "test-api-key",
                "STOPLIGA_STATUS_URL": "dns://blocked.dns.hayahora.futbol",
                "STOPLIGA_IP_LIST_URL": "https://raw.githubusercontent.com/example/repo/main/ip_list.txt",
            },
        )
        self.assertEqual(config.status_url, "dns://blocked.dns.hayahora.futbol")

    def test_legacy_firewall_backend_env_maps_to_opnsense_router_type(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        config = load_config(
            args,
            {
                "STOPLIGA_FIREWALL_BACKEND": "opnsense",
                "OPNSENSE_HOST": "10.0.0.3",
                "OPNSENSE_API_KEY": "test-opnsense-key",
                "OPNSENSE_API_SECRET": "test-opnsense-secret",
            },
        )
        self.assertEqual(config.firewall_backend, "opnsense")
        self.assertEqual(config.router_type, "opnsense")
        self.assertEqual(config.opnsense_host, "10.0.0.3")
        self.assertEqual(config.opnsense_api_key, "test-opnsense-key")
        self.assertEqual(config.opnsense_api_secret, "test-opnsense-secret")
        self.assertIsNone(config.host)
        self.assertIsNone(config.api_key)

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

    def test_state_related_files_must_use_distinct_paths(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = str(Path(tmpdir) / "shared.json")
            with self.assertRaises(ConfigError):
                load_config(
                    args,
                    {
                        "UNIFI_HOST": "10.0.0.2",
                        "UNIFI_API_KEY": "test-api-key",
                        "STOPLIGA_STATE_FILE": shared_path,
                        "STOPLIGA_LOCK_FILE": shared_path,
                    },
                )
