from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from webui.server import create_app  # noqa: E402


class WebUiApiStateTests(unittest.TestCase):
    def test_missing_state_file_returns_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(Path(tmpdir) / "state.json")
            client = app.test_client()
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertEqual(data["status"], "pending")
            self.assertIsNone(data["age_seconds"])

    def test_valid_state_file_returns_data_with_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                json.dumps({
                    "status": "success",
                    "last_success_at": "2026-04-16T10:00:00+00:00",
                    "last_is_blocked": True,
                    "desired_destinations": 142,
                }),
                encoding="utf-8",
            )
            app = create_app(state_file)
            client = app.test_client()
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertEqual(data["status"], "success")
            self.assertTrue(data["last_is_blocked"])
            self.assertEqual(data["desired_destinations"], 142)
            self.assertIsInstance(data["age_seconds"], int)

    def test_malformed_state_file_returns_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text("not valid json", encoding="utf-8")
            app = create_app(state_file)
            client = app.test_client()
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertEqual(data["status"], "error")
            self.assertIn("message", data)
            self.assertIsNone(data["age_seconds"])

    def test_index_returns_html_containing_stopliga(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(Path(tmpdir) / "state.json")
            client = app.test_client()
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"StopLiga", response.data)

    def test_state_without_last_success_has_null_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                json.dumps({"status": "error", "last_success_at": None}),
                encoding="utf-8",
            )
            app = create_app(state_file)
            client = app.test_client()
            response = client.get("/api/state")
            data = json.loads(response.data)
            self.assertIsNone(data["age_seconds"])
