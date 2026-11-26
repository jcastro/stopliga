"""Behavioral regressions from the runtime/resource audit."""

from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
import gzip
import io
import json
import logging
import urllib.error
from unittest.mock import MagicMock, Mock, patch

import pytest

from stopliga.config import build_parser, load_config, validate_config
from stopliga.errors import (
    ConfigError,
    DiscoveryError,
    HTTPStatusError,
    DuplicateRouteError,
    InvalidFeedError,
    NetworkError,
    PartialUpdateError,
    ReconciliationRequiredError,
    RemoteRequestError,
    StaleFeedError,
    StateError,
    UnsupportedRouteShapeError,
)
from stopliga.feed import compact_history, fetch_text, load_feed_snapshot, validate_feed_freshness
from stopliga.healthcheck import main as main_healthcheck, write_healthcheck_settings
from stopliga.models import Config, FeedSnapshot, SiteContext, SyncResult
from stopliga.notifier import NotificationState
from stopliga.opnsense import OPNsenseClient, sync_opnsense
from stopliga.routers.omada import OmadaClient, OmadaRouterDriver
from stopliga.routers.unifi import (
    UniFiClient,
    UniFiRouterDriver,
    V2TrafficRoutesBackend,
    apply_plan,
    choose_existing_route_backend,
)
from stopliga.service import StopLigaService
from stopliga.state import StateStore
from stopliga.schedule import ActiveSchedule, inactive_snapshot


def feed(enabled=True):
    return FeedSnapshot(enabled, enabled, ["192.0.2.1"], {}, 1, 1, 0, [], "h", "h")


def result(dry_run=False):
    return SyncResult(
        "local", "StopLiga", "r", "test", True, False, dry_run, True, False, 1, 1, 0, "h", "h", "ok", is_blocked=True
    )


def config_in(tmp_path, **kwargs):
    return Config(state_file=tmp_path / "state.json", bootstrap_guard_file=tmp_path / "guard.json", **kwargs)


@pytest.mark.parametrize(
    ("stamp", "error_class", "message"),
    [
        (
            "2026-01-01T12:00:00Z",
            StaleFeedError,
            r"is stale .*lastUpdate=2026-01-01T12:00:00\+00:00, age=\d+s, limit=3600s",
        ),
        ("2026-04-23T10:30:00Z", InvalidFeedError, r"is in the future .*ahead=3600s"),
        ("2026-04-23T08:29:59.900000Z", StaleFeedError, r"is stale .*age=3601s, limit=3600s"),
        ("2026-04-23T09:35:00.100000Z", InvalidFeedError, r"is in the future .*ahead=301s"),
        ("bad", InvalidFeedError, "requires a valid lastUpdate"),
        (None, InvalidFeedError, "requires a valid lastUpdate"),
    ],
)
def test_stale_future_and_missing_feed_dates_are_rejected(stamp, error_class, message):
    with pytest.raises(error_class, match=message) as caught:
        validate_feed_freshness({"lastUpdate": stamp, "data": []}, 3600)
    assert type(caught.value) is error_class


def test_loop_throttles_stale_feed_warnings_across_network_errors(tmp_path, caplog):
    service = StopLigaService(config_in(tmp_path, run_mode="loop"))
    stop_event = Mock()
    stop_event.is_set.return_value = False
    stop_event.wait.side_effect = [False] * 6 + [True]
    stale = [StaleFeedError(f"stale attempt {number}") for number in range(5)]
    network_error = NetworkError("fetch failed")

    with (
        patch.object(
            service, "run_once", side_effect=[stale[0], stale[1], stale[2], None, stale[3], network_error, stale[4]]
        ) as run_once,
        patch("stopliga.service.monotonic", side_effect=[0.0, 300.0, 3600.0, 3900.0, 4500.0]),
    ):
        assert service.run_loop(stop_event) == 0

    assert run_once.call_count == 7
    assert [
        (record.levelno, record.fields["error"]) for record in caplog.records if record.msg == "loop_iteration_failed"
    ] == [
        (logging.WARNING, stale[0]),
        (logging.WARNING, stale[2]),
        (logging.WARNING, stale[3]),
        (logging.ERROR, network_error),
    ]


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
    client.get_alias_item.side_effect = [{"content": "192.0.2.9"}, {"content": "192.0.2.1"}, {"content": "192.0.2.9"}]
    client.toggle_rule.side_effect = RemoteRequestError("explicit rejection")
    with pytest.raises(PartialUpdateError) as caught:
        sync_opnsense(config, feed(), client=client)
    assert caught.value.rollback_attempted is True
    assert caught.value.rollback_completed is True
    assert [call.args[-1] for call in client.update_alias_content.call_args_list] == [["192.0.2.1"], ["192.0.2.9"]]
    assert client.get_alias_item.call_count == 3


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
    assert parsed.payload == payload
    assert parsed.inspected_entries == 1


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


def test_opnsense_requests_all_matches_and_rejects_truncated_inventory():
    client = OPNsenseClient(Config(opnsense_host="fw.invalid"))
    with patch.object(
        client, "request", return_value={"rows": [{"description": "StopLiga", "uuid": "r"}], "total": 2}
    ) as request:
        with pytest.raises(RemoteRequestError, match="incomplete inventory"):
            client.search_rule("StopLiga")
    assert "rowCount=-1" in request.call_args.args[1]


@pytest.mark.parametrize("alias_leftover", [False, True])
def test_opnsense_verifies_removal_of_created_alias_during_rollback(alias_leftover):
    from stopliga.opnsense import sync_opnsense

    client = Mock()
    client.search_alias.side_effect = [None, {"uuid": "new"} if alias_leftover else None]
    client.search_rule.return_value = {"uuid": "r", "enabled": "0"}
    client.create_alias.return_value = "new"
    client.reconfigure_alias.side_effect = [RemoteRequestError("reload rejected"), None]
    with pytest.raises(PartialUpdateError) as caught:
        sync_opnsense(Config(router_type="opnsense"), feed(), client=client)
    assert caught.value.rollback_completed is (not alias_leftover)
    client.delete_alias.assert_called_once_with("new")


def test_opnsense_never_enables_rule_with_unverified_alias():
    from stopliga.opnsense import sync_opnsense

    client = Mock()
    client.search_alias.return_value = {"uuid": "a"}
    client.search_rule.return_value = {"uuid": "r", "enabled": "0"}
    client.get_alias_item.return_value = {"content": "192.0.2.9"}
    with pytest.raises(PartialUpdateError) as caught:
        sync_opnsense(Config(router_type="opnsense"), feed(), client=client)
    assert caught.value.rollback_completed is True
    client.toggle_rule.assert_not_called()


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("chunked", [False, True])
def test_feed_retries_a_response_cut_short(history, chunked):
    body = b'{"lastUpdate":"2026-04-23T09:30:00Z","data":[]}'
    complete = io.BytesIO(body)
    complete.headers = {"Content-Length": str(len(body))}
    if chunked:
        truncated = MagicMock()
        truncated.headers = {"Transfer-Encoding": "chunked"}
        truncated.__enter__.return_value = truncated
        truncated.read.side_effect = IncompleteRead(b"", len(body))
    else:
        truncated = io.BytesIO(body[:-3])
        truncated.headers = complete.headers
    opener = Mock()
    opener.open.side_effect = [truncated, complete]
    with (
        patch("stopliga.feed.urllib.request.build_opener", return_value=opener),
        patch("stopliga.feed.sleep_with_backoff"),
    ):
        value = fetch_text(
            "https://feed.invalid", timeout=1, retries=2, verify_tls=True, max_bytes=1024, history=history
        )
    assert opener.open.call_count == 2
    assert (value.payload if history else json.loads(value)) == json.loads(body)


def test_unifi_does_not_retry_a_write_with_an_incomplete_response():
    client = UniFiClient(Config(host="router.invalid", api_key="dummy", retries=4))
    client.opener = MagicMock()
    response = client.opener.open.return_value.__enter__.return_value
    response.headers = {}
    response.read.side_effect = IncompleteRead(b"", 10)
    with pytest.raises(NetworkError):
        client.request("PUT", "/routes/test", json_body={"enabled": False}, retriable=True)
    assert client.opener.open.call_count == 1


def test_unifi_does_not_treat_an_incomplete_inventory_as_absence():
    client = Mock()
    primary = Mock(backend_name="v2")
    primary.list_routes.side_effect = NetworkError("route listing timed out")
    legacy = Mock(backend_name="legacy")
    legacy.list_routes.return_value = "/legacy/routes", []
    with patch("stopliga.routers.unifi.available_backends", return_value=[primary, legacy]):
        with pytest.raises(DiscoveryError, match="timed out"):
            choose_existing_route_backend(client, SiteContext("default"), "StopLiga")


def test_unifi_still_reads_legacy_routes_when_v2_is_unsupported():
    client = Mock()
    primary = Mock(backend_name="v2")
    primary.list_routes.side_effect = HTTPStatusError(404, "endpoint not available")
    legacy = Mock(backend_name="legacy")
    expected = {"_id": "r", "description": "StopLiga"}
    legacy.list_routes.return_value = "/legacy/routes", [expected]
    with patch("stopliga.routers.unifi.available_backends", return_value=[primary, legacy]):
        assert choose_existing_route_backend(client, SiteContext("default"), "StopLiga") == (
            legacy,
            "/legacy/routes",
            expected,
        )


def test_interrupted_scheduled_close_keeps_pending_write_and_notification_ack(tmp_path):
    config = config_in(tmp_path, active_hours="20:00-21:00", gotify_url="https://gotify.invalid", gotify_token="dummy")
    store = StateStore(config.state_file)
    service = StopLigaService(config)
    store.write_payload({"last_is_blocked": False})

    class Driver:
        def sync(self, snapshot, guard, *, before_write, **kwargs):
            before_write()
            if not snapshot.desired_enabled:
                raise SystemExit("simulated stop after the closing write")
            return result()

    with (
        patch.object(service.schedule, "is_active", side_effect=[True, True, True, False]),
        patch("stopliga.service.load_feed_snapshot", return_value=feed()),
        patch("stopliga.service.create_router_driver", return_value=Driver()),
        patch("stopliga.service.send_notifications", return_value=NotificationState(delivered_providers=("gotify",))),
        pytest.raises(SystemExit),
    ):
        service.run_once()

    state = store.load()
    assert state["status"] == "applying"
    assert state["reconciliation_required"] is True
    assert state["last_notified_blocked"] == {"gotify": True}
    assert state["notification_pending"] == {}
    assert not store.healthcheck(900)[0]
    with pytest.raises(ReconciliationRequiredError):
        StopLigaService(config).run_once()


def test_corrupt_bootstrap_guard_recovers_manual_review_from_state(tmp_path):
    config = config_in(tmp_path)
    config.bootstrap_guard_file.write_text("{bad")
    saved = {
        "route_name": config.route_name,
        "site": config.site,
        "bootstrap_source": "auto-bootstrap-device-fallback",
        "bootstrap_network_id": "test-network",
        "bootstrap_target_macs": ["aa:bb:cc:dd:ee:01"],
    }
    StateStore(config.state_file).write_payload(saved)
    service = StopLigaService(config)
    route = {"network_id": "test-network", "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01"}]}
    guard = service._load_bootstrap_guard()
    assert UniFiRouterDriver(config)._is_pending_auto_bootstrap(route, guard)
    assert not config.bootstrap_guard_file.exists()
    assert len(list(tmp_path.glob("guard.json.bad-*"))) == 1


def test_unrecoverable_bootstrap_guard_stays_blocked_on_restart(tmp_path):
    config = config_in(tmp_path)
    config.bootstrap_guard_file.write_text("{bad")
    for _ in range(2):
        with pytest.raises(StateError, match="Cannot safely read bootstrap guard"):
            StopLigaService(config)._load_bootstrap_guard()
        assert config.bootstrap_guard_file.read_text() == "{bad"


def test_image_healthcheck_uses_the_effective_state_path_and_loop_age(tmp_path, monkeypatch):
    settings = tmp_path / "health.json"
    default_state = tmp_path / "default.json"
    actual_state = tmp_path / "actual.json"
    monkeypatch.setenv("STOPLIGA_HEALTHCHECK_FILE", str(settings))
    monkeypatch.setenv("STOPLIGA_STATE_FILE", str(default_state))
    now = datetime.now(timezone.utc)
    StateStore(default_state).write_payload({"status": "success", "last_success_at": now.isoformat()})
    StateStore(actual_state).write_payload(
        {"status": "success", "last_success_at": (now - timedelta(minutes=20)).isoformat()}
    )
    config = load_config(build_parser().parse_args(["--loop", "--state-file", str(actual_state)]), validate=False)
    write_healthcheck_settings(config)
    assert main_healthcheck() == 1
    StateStore(actual_state).write_payload({"status": "success", "last_success_at": now.isoformat()})
    assert main_healthcheck() == 0


def test_history_omits_inactive_rows_but_retains_provider_and_freshness_checks():
    inactive = {
        "ip": "192.0.2.1",
        "isp": "Known ISP",
        "stateChanges": [{"timestamp": "2026-04-23T09:00:00Z", "state": "false"}],
    }
    active = {
        "ip": "192.0.2.2",
        "isp": "Another ISP",
        "stateChanges": [{"timestamp": "2026-04-23T09:00:00Z", "state": True}],
    }
    payload = {"lastUpdate": "2026-04-23T09:15:00Z", "isBlocked": True, "data": [inactive, active]}
    with patch("stopliga.feed.fetch_text", return_value=json.dumps(payload)):
        snapshot = load_feed_snapshot(Config(hayahora_isp="known isp"))
    assert snapshot.destinations == []
    assert snapshot.raw_line_count == 2
    assert snapshot.is_blocked is True
    assert snapshot.desired_enabled is False

    inactive["stateChanges"][0]["timestamp"] = "2026-04-23T09:25:00Z"
    with patch("stopliga.feed.fetch_text", return_value=json.dumps(payload)):
        with pytest.raises(InvalidFeedError, match="future state change"):
            load_feed_snapshot(Config(hayahora_isp="Another ISP"))


def test_opnsense_cannot_verify_disabled_rule_without_enabled_field():
    client = Mock()
    client.search_alias.return_value = None
    original = {"uuid": "r", "enabled": "1"}
    client.search_rule.side_effect = [original, {"uuid": "r"}, original]
    with pytest.raises(PartialUpdateError, match="valid enabled state") as caught:
        sync_opnsense(Config(router_type="opnsense"), inactive_snapshot(), client=client)
    assert caught.value.rollback_completed is True


def test_unifi_records_single_device_guard_before_fallback_creation():
    driver = UniFiRouterDriver(Config())
    client = Mock()
    client.resolve_site_context.return_value = SiteContext("default")
    client.pick_default_vpn_network.return_value = {"_id": "test-network"}
    client.pick_default_target_device.return_value = {"type": "CLIENT", "client_mac": "aa:bb:cc:dd:ee:01"}
    backend = Mock(backend_name="v2")
    guard_writer = Mock()
    guard_clearer = Mock()

    def create_route(payload):
        if payload["target_devices"] == [{"type": "ALL_CLIENTS"}]:
            raise HTTPStatusError(400, "ALL_CLIENTS rejected")
        guard_writer.assert_called_with("auto-bootstrap-device-fallback", "test-network", ("aa:bb:cc:dd:ee:01",))
        raise NetworkError("creation response lost")

    backend.create_route.side_effect = create_route
    with (
        patch("stopliga.routers.unifi.UniFiClient", return_value=client),
        patch("stopliga.routers.unifi._discover_route", return_value=(backend, "/routes", None)),
        pytest.raises(PartialUpdateError, match="fallback creation outcome"),
    ):
        driver.sync(feed(), {}, guard_writer=guard_writer, guard_clearer=guard_clearer)
    assert backend.create_route.call_count == 2
    guard_clearer.assert_not_called()
