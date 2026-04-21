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
