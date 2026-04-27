from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stopliga.errors import ConfigError  # noqa: E402
from stopliga.models import StateSnapshot  # noqa: E402
from stopliga.state import MAX_STATE_FILE_BYTES, StateStore  # noqa: E402


class StateStoreHardeningTests(unittest.TestCase):
    def test_load_rejects_oversized_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("x" * (MAX_STATE_FILE_BYTES + 1), encoding="utf-8")

            with self.assertRaises(ConfigError):
                StateStore(path).load()

    def test_write_sets_restrictive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = StateStore(path)
            store.write(
                StateSnapshot(
                    status="success",
                    run_mode="once",
                    route_name="StopLiga",
                    site="default",
                    last_attempt_at="2026-04-21T00:00:00+00:00",
                    last_success_at="2026-04-21T00:00:00+00:00",
                    last_error=None,
                    last_mode="local",
                    last_sync_id="sync-1",
                    last_route_id="route-1",
                    last_backend="unifi",
                    feed_hash="feed",
                    destinations_hash="dest",
                    changed=False,
                    created=False,
                    dry_run=False,
                )
            )

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_write_persists_tuple_fields_as_json_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = StateStore(path)
            store.write(
                StateSnapshot(
                    status="guard",
                    run_mode="once",
                    route_name="StopLiga",
                    site="default",
                    last_attempt_at="2026-04-21T00:00:00+00:00",
                    last_success_at=None,
                    last_error=None,
                    last_mode=None,
                    last_sync_id=None,
                    last_route_id=None,
                    last_backend=None,
                    feed_hash=None,
                    destinations_hash=None,
                    changed=False,
                    created=False,
                    dry_run=False,
                    bootstrap_target_macs=("aa:bb:cc:dd:ee:ff",),
                )
            )

            self.assertEqual(store.load()["bootstrap_target_macs"], ["aa:bb:cc:dd:ee:ff"])
