from dataclasses import replace
from datetime import datetime
import json
from unittest.mock import Mock, patch

import pytest

from stopliga.config import build_parser, load_config
from stopliga.errors import ConfigError, RemoteRequestError
from stopliga.models import Config, SyncResult
from stopliga.schedule import ActiveSchedule
from stopliga.service import StopLigaService
from stopliga.routers.unifi import BaseRouteBackend


def active(days=None, hours=None, zone="Europe/Madrid"):
    return ActiveSchedule(Config(active_days=days, active_hours=hours, active_timezone=zone))


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-11-06T19:59:59+01:00", False),
        ("2026-11-06T20:00:00+01:00", True),
        ("2026-11-06T23:59:00+01:00", False),
        ("2026-11-09T21:00:00+01:00", False),
    ],
)
def test_window_boundaries(stamp, expected):
    assert active("fri,sat,sun", "20:00-23:59").is_active(datetime.fromisoformat(stamp)) is expected


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-11-07T01:59:00+01:00", True),
        ("2026-11-07T02:00:00+01:00", False),
        ("2026-11-06T01:00:00+01:00", False),
        ("2026-11-07T23:00:00+01:00", False),
    ],
)
def test_overnight_uses_starting_day(stamp, expected):
    assert active("fri", "22:00-02:00").is_active(datetime.fromisoformat(stamp)) is expected


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-10-25T00:30:00+00:00",  # First occurrence of 02:30.
        "2026-10-25T01:30:00+00:00",  # Second occurrence of 02:30.
        "2026-03-29T01:00:00+00:00",  # Missing spring hour advances to 03:00.
    ],
)
def test_dst_uses_wall_clock(stamp):
    assert active("sun", "02:00-04:00").is_active(datetime.fromisoformat(stamp))


def test_optional_days_hours_and_multiple_windows():
    monday = datetime.fromisoformat("2026-11-09T12:00:00+01:00")
    assert active().is_active(monday)
    assert not active("sun").is_active(monday)
    assert active(hours="11:00-13:00,20:00-22:00").is_active(monday)
    assert active().wait_seconds(300) == 300
    assert 0 < active("mon").wait_seconds(300) <= 60


@pytest.mark.parametrize(
    "settings",
    [
        {"active_days": "fri,xyz"},
        {"active_days": "fri,"},
        {"active_days": 1},
        {"active_hours": "24:00-02:00"},
        {"active_hours": "20:60-23:00"},
        {"active_hours": "20:00-20:00"},
        {"active_hours": "bad"},
        {"active_timezone": "invalid/zone"},
        {"active_timezone": ""},
    ],
)
def test_invalid_schedule_rejected(settings):
    with pytest.raises(ConfigError):
        ActiveSchedule(replace(Config(), **settings))


def test_config_precedence(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text('[app]\nactive_days="mon"\nactive_hours="10:00-11:00"\nactive_timezone="UTC"\n')
    config = load_config(
        build_parser().parse_args(["--config", str(file), "--active-days", "sun"]),
        {"STOPLIGA_ACTIVE_DAYS": "fri", "STOPLIGA_ACTIVE_TIMEZONE": "Europe/Madrid"},
        validate=False,
    )
    assert (config.active_days, config.active_hours, config.active_timezone) == ("sun", "10:00-11:00", "Europe/Madrid")


def test_outside_window_disables_even_with_unavailable_feed(tmp_path):
    config = Config(active_days="mon", state_file=tmp_path / "state.json", bootstrap_guard_file=tmp_path / "guard.json")
    service = StopLigaService(config)
    driver = Mock()
    driver.sync.return_value = SyncResult(
        mode="local",
        route_name="StopLiga",
        route_id="1",
        backend_name="test",
        changed=True,
        created=False,
        dry_run=False,
        desired_enabled=False,
        current_enabled=True,
        desired_destinations=0,
        current_destinations=1,
        invalid_entries=0,
        feed_hash="schedule-inactive",
        destinations_hash="schedule-inactive",
        summary="disabled",
    )
    with (
        patch.object(service.schedule, "is_active", return_value=False),
        patch.object(service, "_load_runtime_state", return_value={"last_is_blocked": True}),
        patch("stopliga.service.load_feed_snapshot", side_effect=AssertionError("feed must not be fetched")),
        patch("stopliga.service.create_router_driver", return_value=driver),
        patch("stopliga.service.send_notifications") as notifications,
    ):
        service.run_once()
    snapshot = driver.sync.call_args.args[0]
    assert not snapshot.desired_enabled
    assert snapshot.destinations == []
    notifications.assert_not_called()
    assert json.loads(config.state_file.read_text())["last_is_blocked"] is True


def test_verification_rejects_missing_enabled_state():
    backend = object.__new__(BaseRouteBackend)
    backend.get_route = Mock(return_value=("/routes", {"_id": "1", "description": "test"}))
    with pytest.raises(RemoteRequestError):
        backend.verify("1", [], False)


def test_unifi_http_success_with_application_error_is_rejected():
    from unittest.mock import MagicMock
    from stopliga.routers.unifi import UniFiClient

    client = UniFiClient(Config(host="127.0.0.1", api_key="test"))
    response = MagicMock()
    response.status = 200
    response.headers = {}
    response.read.side_effect = [b'{"meta":{"rc":"error","msg":"api.err.InvalidObject"}}', b""]
    client.opener = MagicMock()
    client.opener.open.return_value.__enter__.return_value = response
    with pytest.raises(RemoteRequestError, match="application-level"):
        client.request("GET", "/test")


def test_created_route_is_verified_even_without_followup_changes():
    from stopliga.models import FeedSnapshot
    from stopliga.routers.unifi import UniFiRouterDriver, V2TrafficRoutesBackend

    client = Mock()
    client.config = Config()
    backend = V2TrafficRoutesBackend(client, Mock())
    backend.verify = Mock(side_effect=RemoteRequestError("not persisted"))
    route = {
        "_id": "1",
        "description": "StopLiga",
        "enabled": False,
        "ip_addresses": [{"ip_or_subnet": "192.0.2.1", "ip_version": "v4"}],
    }
    feed = FeedSnapshot(False, False, ["192.0.2.1"], {}, 1, 1, 0, [], "hash", "hash")
    with pytest.raises(RemoteRequestError, match="not persisted"):
        UniFiRouterDriver(Config())._plan_route_update(
            backend=backend,
            endpoint="/routes",
            route_record=route,
            feed_snapshot=feed,
            previous_guard={},
            created=True,
            bootstrap_source=None,
            bootstrap_network_id=None,
            bootstrap_target_macs=(),
            client=client,
        )
    backend.verify.assert_called_once_with("1", ["192.0.2.1"], False)


def test_feed_fetch_crossing_closing_boundary_disables(tmp_path):
    from stopliga.models import FeedSnapshot

    service = StopLigaService(
        Config(
            active_hours="20:00-21:00", state_file=tmp_path / "state.json", bootstrap_guard_file=tmp_path / "guard.json"
        )
    )
    feed = FeedSnapshot(True, True, ["192.0.2.1"], {}, 1, 1, 0, [], "hash", "hash")
    result = SyncResult(
        mode="local",
        route_name="StopLiga",
        route_id="1",
        backend_name="test",
        changed=True,
        created=False,
        dry_run=False,
        desired_enabled=False,
        current_enabled=True,
        desired_destinations=0,
        current_destinations=1,
        invalid_entries=0,
        feed_hash="hash",
        destinations_hash="hash",
        summary="disabled",
    )
    with (
        patch.object(service.schedule, "is_active", side_effect=[True, False]),
        patch("stopliga.service.load_feed_snapshot", return_value=feed),
        patch.object(service, "_run_router_sync", return_value=result) as sync,
        patch("stopliga.service.send_notifications") as notifications,
    ):
        service.run_once()
    assert sync.call_args.args[0].desired_enabled is False
    assert sync.call_args.args[0].destinations == []
    notifications.assert_not_called()


def test_loop_caps_wait_only_when_schedule_is_configured(tmp_path):
    from stopliga.errors import NetworkError

    for days, upper_bound in [(None, 300), ("mon", 60)]:
        service = StopLigaService(
            Config(
                active_days=days,
                interval_seconds=300,
                state_file=tmp_path / "state.json",
                bootstrap_guard_file=tmp_path / "guard.json",
            )
        )
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(service, "run_once", side_effect=NetworkError("offline")):
            assert service.run_loop(stop) == 0
        wait = stop.wait.call_args.args[0]
        assert 0 < wait <= upper_bound
        if days is None:
            assert wait == 300
