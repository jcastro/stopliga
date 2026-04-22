from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = ROOT / "docker" / "entrypoint.py"

_SPEC = importlib.util.spec_from_file_location("stopliga_docker_entrypoint", ENTRYPOINT_PATH)
assert _SPEC and _SPEC.loader
entrypoint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(entrypoint)


class DockerEntrypointTests(unittest.TestCase):
    def test_env_int_rejects_non_integer_values(self) -> None:
        with mock.patch.dict("os.environ", {"STOPLIGA_UID": "abc"}, clear=False):
            with self.assertRaises(ValueError):
                entrypoint._env_int("STOPLIGA_UID", 10001)

    def test_candidate_paths_include_runtime_directories_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            lock_file = Path(tmpdir) / "stopliga.lock"
            guard_file = Path(tmpdir) / "bootstrap_guard.json"
            with mock.patch.dict(
                "os.environ",
                {
                    "STOPLIGA_STATE_FILE": str(state_file),
                    "STOPLIGA_LOCK_FILE": str(lock_file),
                    "STOPLIGA_BOOTSTRAP_GUARD_FILE": str(guard_file),
                },
                clear=False,
            ):
                candidates = entrypoint._candidate_paths()

        self.assertIn((state_file.parent, False), candidates)
        self.assertIn((state_file, True), candidates)
        self.assertIn((lock_file, True), candidates)
        self.assertIn((guard_file, True), candidates)

    def test_default_config_file_is_enabled_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.toml"
            config_file.write_text('[app]\nbackend = "unifi"\n', encoding="utf-8")
            with (
                mock.patch.object(entrypoint, "DEFAULT_CONFIG_FILE", config_file),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                entrypoint._maybe_enable_default_config_file()
                self.assertEqual(str(config_file), entrypoint.os.environ["STOPLIGA_CONFIG_FILE"])

    def test_existing_config_env_wins_over_default_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.toml"
            config_file.write_text('[app]\nbackend = "unifi"\n', encoding="utf-8")
            with (
                mock.patch.object(entrypoint, "DEFAULT_CONFIG_FILE", config_file),
                mock.patch.dict("os.environ", {"STOPLIGA_CONFIG_FILE": "/custom/config.toml"}, clear=True),
            ):
                entrypoint._maybe_enable_default_config_file()
                self.assertEqual("/custom/config.toml", entrypoint.os.environ["STOPLIGA_CONFIG_FILE"])
