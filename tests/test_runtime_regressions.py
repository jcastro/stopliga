"""Behavioral regressions from the runtime/resource audit."""

from datetime import datetime, timezone
import gzip
import io
import json
import urllib.error
from unittest.mock import MagicMock, Mock, patch

import pytest

from stopliga.config import build_parser, load_config, validate_config
from stopliga.errors import (
    ConfigError,
    DuplicateRouteError,
    InvalidFeedError,
    NetworkError,
    PartialUpdateError,
    ReconciliationRequiredError,
    RemoteRequestError,
    UnsupportedRouteShapeError,
)
from stopliga.feed import compact_history, fetch_text, load_feed_snapshot, validate_feed_freshness
from stopliga.models import Config, FeedSnapshot, SiteContext, SyncResult
from stopliga.opnsense import OPNsenseClient
from stopliga.routers.omada import OmadaClient, OmadaRouterDriver
from stopliga.routers.unifi import UniFiClient, UniFiRouterDriver, V2TrafficRoutesBackend, apply_plan
from stopliga.service import StopLigaService
from stopliga.state import StateStore
from stopliga.schedule import ActiveSchedule


def feed(enabled=True):
    return FeedSnapshot(enabled, enabled, ["192.0.2.1"], {}, 1, 1, 0, [], "h", "h")


def result(dry_run=False):
    return SyncResult(
        "local", "StopLiga", "r", "test", True, False, dry_run, True, False, 1, 1, 0, "h", "h", "ok", is_blocked=True
    )


def config_in(tmp_path, **kwargs):
    return Config(state_file=tmp_path / "state.json", bootstrap_guard_file=tmp_path / "guard.json", **kwargs)


@pytest.mark.parametrize("stamp", ["2026-01-01T12:00:00Z", "2026-04-23T10:30:00Z", "bad", None])
def test_stale_future_and_missing_feed_dates_are_rejected(stamp):
    with pytest.raises(InvalidFeedError):
        validate_feed_freshness({"lastUpdate": stamp, "data": []}, 3600)


def test_history_compaction_retains_latest_timestamp_even_if_unsorted():
    payload = {
        "lastUpdate": "2026-04-23T09:16:40Z",
        "data": [
            {
                "ip": "192.0.2.1",
                "stateChanges": [
                    {"state": True, "timestamp": "2026-04-23T09:00:00Z"},
                    {"state": False, "timestamp": "2026-04-22T09:00:00Z"},
                ],
            }
        ],
    }
    compact = compact_history(json.dumps(payload))
    assert compact["data"][0]["stateChanges"] == [payload["data"][0]["stateChanges"][0]]
    validate_feed_freshness(compact, 3600)


def test_future_history_change_is_rejected():
    with pytest.raises(InvalidFeedError, match="future state"):
        validate_feed_freshness(
            {"lastUpdate": "2026-04-23T09:16:40Z", "data": [{"stateChanges": [{"timestamp": "2026-04-23T10:00:00Z"}]}]},
            3600,
        )


@pytest.mark.parametrize("raw", ["[]", '{"data":[],"data":[]}', '{"isBlocked":true} {"isBlocked":false}', "{bad"])
def test_invalid_or_ambiguous_status_shape_is_rejected(raw):
    with pytest.raises(InvalidFeedError):
        compact_history(raw)


@pytest.mark.parametrize(
    "expanded,limit,expected", [(b'{"blocked":true}', 1024, '{"blocked":true}'), (b"x" * 10000, 1024, None)]
)
def test_gzip_support_and_expansion_ceiling(expanded, limit, expected):
    response = io.BytesIO(gzip.compress(expanded))
    response.headers = {"Content-Encoding": "gzip"}
    opener = Mock()
    opener.open.return_value = response
    with patch("stopliga.feed.urllib.request.build_opener", return_value=opener):
        if expected is None:
            with pytest.raises(NetworkError, match="compressed"):
                fetch_text("https://feed.invalid", timeout=1, retries=1, verify_tls=True, max_bytes=limit)
        else:
            assert (
                fetch_text("https://feed.invalid", timeout=1, retries=1, verify_tls=True, max_bytes=limit) == expected
            )
        assert opener.open.call_args.args[0].get_header("Accept-encoding") == "gzip"


def test_legacy_status_and_txt_destinations_are_used():
    config = Config(status_url="https://custom.invalid/status", ip_list_url="https://custom.invalid/ips")
    with patch("stopliga.feed.fetch_text", side_effect=['{"blocked":true}', "192.0.2.1\n192.0.2.1\n"]):
        assert load_feed_snapshot(config).destinations == ["192.0.2.1"]
    with patch("stopliga.feed.fetch_text", side_effect=['{"blocked":true}', ""]):
        with pytest.raises(InvalidFeedError):
            load_feed_snapshot(config)


def test_cli_router_type_has_priority_over_legacy_environment():
    config = load_config(
        build_parser().parse_args(["--router-type", "unifi"]),
        environ={"STOPLIGA_FIREWALL_BACKEND": "opnsense"},
        validate=False,
    )
    assert config.router_type == "unifi"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_timeouts_rejected(value):
    with pytest.raises(ConfigError):
        validate_config(
            Config(host="router.invalid", api_key="dummy", request_timeout=value), validate_connection=False
        )


def test_guard_survives_multiple_cycles_and_restart():
    config = Config(dry_run=True)
    client = Mock(config=config)
    backend = V2TrafficRoutesBackend(client, SiteContext("default"))
    route = {
        "_id": "r",
        "description": "StopLiga",
        "enabled": False,
        "network_id": "n",
        "target_devices": [{"type": "CLIENT", "client_mac": "aa:bb:cc:dd:ee:01"}],
        "ip_addresses": [{"ip_or_subnet": "192.0.2.1", "ip_version": "v4"}],
    }
    guard = {
        "bootstrap_source": "auto-bootstrap-device-fallback",
        "bootstrap_network_id": "n",
        "bootstrap_target_macs": ["aa:bb:cc:dd:ee:01"],
    }
    for _ in range(4):
        outcome = UniFiRouterDriver(config)._plan_route_update(
            backend=backend,
            endpoint="/routes",
            route_record=route,
            feed_snapshot=feed(),
            previous_guard=guard,
            created=False,
            bootstrap_source=None,
            bootstrap_network_id=None,
            bootstrap_target_macs=(),
            client=client,
        )
        assert outcome.desired_enabled is False
        assert outcome.bootstrap_source == "auto-bootstrap-device-fallback"
        guard = json.loads(
            json.dumps(
                {
                    key: getattr(outcome, key)
                    for key in ("bootstrap_source", "bootstrap_network_id", "bootstrap_target_macs")
                }
            )
        )


def test_ambiguous_write_requires_reconciliation():
    client = Mock(config=Config())
    backend = V2TrafficRoutesBackend(client, SiteContext("default"))
    route = {
        "_id": "r",
        "description": "StopLiga",
        "enabled": False,
        "ip_addresses": [{"ip_or_subnet": "192.0.2.1", "ip_version": "v4"}],
    }
    plan = backend.build_plan("/routes", route, ["192.0.2.1"], True)
    client.request.side_effect = NetworkError("applied, response lost")
    with pytest.raises(PartialUpdateError) as caught:
        apply_plan(client, backend, plan)
    assert StopLigaService._requires_reconciliation(caught.value, reconciliation_pending=False)


def test_pending_write_is_durable_before_io_and_blocks_restart(tmp_path):
    config = config_in(tmp_path)

    class Driver:
        def sync(self, snapshot, guard, *, before_write, **kwargs):
            before_write()
            pending = StateStore(config.state_file).load()
            assert pending["status"] == "applying"
            assert pending["reconciliation_required"] is True
            raise SystemExit("simulated process death after router write")

    service = StopLigaService(config)
    with (
        patch("stopliga.service.load_feed_snapshot", return_value=feed()),
        patch("stopliga.service.create_router_driver", return_value=Driver()),
    ):
        with pytest.raises(SystemExit):
            service.run_once()
    with pytest.raises(ReconciliationRequiredError):
        StopLigaService(config).run_once()


def test_dry_run_preserves_invalid_state_and_guard(tmp_path):
    config = config_in(tmp_path, dry_run=True)
    config.state_file.write_bytes(b"\xff\xfe")
    config.bootstrap_guard_file.write_text("{bad")
    service = StopLigaService(config)
    with (
        patch("stopliga.service.load_feed_snapshot", return_value=feed()),
        patch.object(service, "_run_router_sync", return_value=result(True)),
    ):
        service.run_once()
    assert config.state_file.read_bytes() == b"\xff\xfe"
    assert config.bootstrap_guard_file.read_text() == "{bad"
    assert len(list(tmp_path.iterdir())) == 2


def test_notifications_retry_only_failed_provider_across_restart(tmp_path):
    config = config_in(
        tmp_path,
        gotify_url="https://gotify.invalid",
        gotify_token="secret",
        ntfy_url="https://ntfy.invalid",
        ntfy_topic="alerts",
    )
    config.state_file.write_text('{"last_is_blocked":false}')
    attempts = {"gotify": 0, "ntfy": 0}

    def post(url, payload, **kwargs):
        name = "gotify" if "gotify" in url else "ntfy"
        attempts[name] += 1
        if name == "gotify":
            assert kwargs["headers"]["X-Gotify-Key"] == "secret"
            if attempts[name] == 1:
                raise NetworkError("provider unavailable")
        return {"id": 123 if name == "gotify" else "msg"}

    with (
        patch("stopliga.service.load_feed_snapshot", return_value=feed()),
        patch("stopliga.notifier._post_json", side_effect=post),
    ):
        for index in range(3):
            service = StopLigaService(config)
            with patch.object(service, "_run_router_sync", return_value=result()):
                service.run_once()
            if index == 0:
                assert StateStore(config.state_file).load()["notification_pending"] == {"gotify": True}
    assert attempts == {"gotify": 2, "ntfy": 1}
    assert StateStore(config.state_file).load()["notification_pending"] == {}


@pytest.mark.parametrize(
    "name,owned", [("StopLiga [001]", True), ("StopLigaBackup [001]", False), ("StopLiga manual [007]", False)]
)
def test_omada_group_ownership_is_exact(name, owned):
    assert OmadaRouterDriver(Config(router_type="omada"))._is_managed_group_name(name) is owned


@pytest.mark.parametrize(
    "failure,expected", [(TimeoutError("timeout"), NetworkError), (ValueError("too large"), RemoteRequestError)]
)
def test_omada_transport_errors_remain_domain_errors(failure, expected):
    client = OmadaClient(Config(omada_base_url="https://omada.invalid", retries=1))
    client.access_token = "dummy"
    with patch("urllib.request.urlopen", side_effect=failure), pytest.raises(expected):
        client.request("GET", "/test")


def test_unifi_get_retry_never_copies_error_body_into_request():
    client = UniFiClient(Config(host="router.invalid", api_key="dummy", retries=2))
    response = MagicMock(status=200, headers={})
    response.read.side_effect = [b"[]", b""]
    client.opener = Mock()
    client.opener.open.side_effect = [
        urllib.error.HTTPError("https://router.invalid", 503, "retry", {}, io.BytesIO(b'{"error":"retry"}')),
        MagicMock(__enter__=Mock(return_value=response)),
    ]
    with patch("stopliga.routers.unifi.sleep_with_backoff"):
        client.request("GET", "/test")
    assert [call.args[0].data for call in client.opener.open.call_args_list] == [None, None]


def test_healthcheck_handles_naive_timestamp_and_corrupt_utf8(tmp_path):
    config = config_in(tmp_path)
    config.state_file.write_text('{"status":"success","last_success_at":"2026-11-08T12:00:00"}')
    assert StateStore(config.state_file).healthcheck(300)[0] is False
    config.state_file.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError):
        StateStore(config.state_file).load()


def test_schedule_waits_for_boundary_without_polling_every_minute():
    schedule = ActiveSchedule(Config(active_hours="20:00-21:00", active_timezone="UTC"))
    with patch("stopliga.schedule.datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 8, 20, 57, 30, tzinfo=timezone.utc)
        assert schedule.wait_seconds(300) == 150
        clock.now.return_value = datetime(2026, 11, 8, 20, 0, 30, tzinfo=timezone.utc)
        assert schedule.wait_seconds(300) == 300


def test_opnsense_rejects_duplicate_rules_and_aliases():
    client = OPNsenseClient(Config(opnsense_host="fw.invalid"))
    with patch.object(client, "request", return_value={"rows": [{"name": "StopLiga"}, {"name": "StopLiga"}]}):
        with pytest.raises(DuplicateRouteError):
            client.search_alias("StopLiga")
    with patch.object(
        client, "request", return_value={"rows": [{"description": "StopLiga"}, {"description": "StopLiga"}]}
    ):
        with pytest.raises(DuplicateRouteError):
            client.search_rule("StopLiga")


def test_opnsense_http200_application_failure_is_rejected():
    client = OPNsenseClient(Config(opnsense_host="fw.invalid"))
    response = MagicMock(status=200, headers={})
    response.read.side_effect = [b'{"result":"failed"}', b""]
    client.opener = MagicMock()
    client.opener.open.return_value.__enter__.return_value = response
    with pytest.raises(RemoteRequestError, match="reported a failure"):
        client.request("POST", "/firewall/filter/apply")


def test_opnsense_restores_and_verifies_alias_after_rule_failure():
    from stopliga.opnsense import sync_opnsense

    config = Config(router_type="opnsense", opnsense_host="fw.invalid")
    client = Mock()
    client.search_alias.return_value = {"uuid": "a"}
    client.search_rule.return_value = {"uuid": "r", "enabled": "0"}
    client.get_alias_item.side_effect = [{"content": "192.0.2.9"}, {"content": "192.0.2.9"}]
    client.toggle_rule.side_effect = RemoteRequestError("explicit rejection")
    with pytest.raises(PartialUpdateError) as caught:
        sync_opnsense(config, feed(), client=client)
    assert caught.value.rollback_attempted is True
    assert caught.value.rollback_completed is True
    assert [call.args[-1] for call in client.update_alias_content.call_args_list] == [["192.0.2.1"], ["192.0.2.9"]]
    assert client.get_alias_item.call_count == 2


def test_omada_pagination_reads_short_pages_when_total_is_known():
    client = OmadaClient(Config(omada_base_url="https://omada.invalid"))
    with patch.object(
        client,
        "request",
        side_effect=[
            {"result": {"data": [{"id": "1"}], "totalRows": 2}},
            {"result": {"data": [{"id": "2"}], "totalRows": 2}},
        ],
    ) as request:
        assert client._paged_records("/test") == [{"id": "1"}, {"id": "2"}]
        assert "page=2&" in request.call_args.args[1]
    with patch.object(client, "request", return_value={"result": {"data": [{"id": "1"}], "totalRows": 2}}):
        with pytest.raises(RemoteRequestError, match="repeated a page"):
            client._paged_records("/test")


@pytest.mark.parametrize("compressed", [False, True])
def test_history_download_parses_the_bounded_stream(compressed):
    payload = {
        "isBlocked": True,
        "lastUpdate": "2026-04-23T09:30:00Z",
        "data": [{"ip": "192.0.2.1", "stateChanges": [{"state": True, "timestamp": "2026-04-23T09:00:00Z"}]}],
    }
    body = json.dumps(payload).encode()
    response = io.BytesIO(gzip.compress(body) if compressed else body)
    response.headers = {"Content-Encoding": "gzip"} if compressed else {}
    opener = Mock()
    opener.open.return_value = response
    with patch("stopliga.feed.urllib.request.build_opener", return_value=opener):
        parsed = fetch_text("https://feed.invalid", timeout=1, retries=1, verify_tls=True, max_bytes=1024, history=True)
    assert parsed == payload


def test_compressed_history_expansion_cannot_bypass_limit():
    body = json.dumps({"data": [], "padding": "x" * 20000}).encode()
    response = io.BytesIO(gzip.compress(body))
    response.headers = {"Content-Encoding": "gzip"}
    opener = Mock()
    opener.open.return_value = response
    with (
        patch("stopliga.feed.urllib.request.build_opener", return_value=opener),
        pytest.raises(NetworkError, match="size limit"),
    ):
        fetch_text("https://feed.invalid", timeout=1, retries=1, verify_tls=True, max_bytes=1024, history=True)


def test_omada_shared_group_is_rejected_before_any_mutation():
    config = Config(router_type="omada", omada_base_url="https://omada.invalid")
    driver = OmadaRouterDriver(config)
    client = Mock()
    client.resolve_site.return_value = Mock(site_id="site-1")
    client.list_groups.return_value = [
        {"groupId": "shared", "name": "StopLiga [001]", "type": 0, "ipList": [{"ip": "192.0.2.9", "mask": 32}]}
    ]
    client.list_policy_routes.return_value = [
        {"id": "ours", "name": "StopLiga", "status": True, "destinationIds": ["shared"]},
        {"id": "manual", "name": "Manual", "status": True, "destinationIds": ["shared"]},
    ]
    with (
        patch("stopliga.routers.omada.OmadaClient", return_value=client),
        patch.object(driver, "_resolve_target"),
        patch.object(driver, "_resolve_source_network_ids", return_value=["lan"]),
        pytest.raises(UnsupportedRouteShapeError, match="another route"),
    ):
        driver.sync(feed(), {}, guard_writer=Mock(), guard_clearer=Mock())
    client.update_group.assert_not_called()
    client.create_group.assert_not_called()


def test_notification_delay_cannot_leave_route_enabled_after_closing(tmp_path):
    from dataclasses import replace

    service = StopLigaService(config_in(tmp_path, active_hours="20:00-21:00"))
    enabled = result()
    disabled = replace(enabled, desired_enabled=False)
    with (
        patch.object(service.schedule, "is_active", side_effect=[True, True, True, False]),
        patch("stopliga.service.load_feed_snapshot", return_value=feed()),
        patch.object(service, "_run_router_sync", side_effect=[enabled, disabled]) as sync,
    ):
        assert service.run_once().desired_enabled is False
    assert sync.call_count == 2
    assert sync.call_args.args[0].desired_enabled is False
