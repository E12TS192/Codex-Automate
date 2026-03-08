from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_automate.state import StateStore
from codex_automate.worker_host import WorkerHostConfig, inspect_worker_host, resolve_worker_host_config


class WorkerHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp_dir.name)
        self.db_path = self.workspace_root / "worker.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_config_parses_environment(self) -> None:
        config = resolve_worker_host_config(
            env={
                "CODEX_AUTOMATE_DATABASE_URL": "postgresql://user:pass@example.com/db",
                "CODEX_AUTOMATE_WORKSPACE": str(self.workspace_root),
                "CODEX_AUTOMATE_POLL_SECONDS": "7.5",
                "CODEX_AUTOMATE_MAX_CYCLES": "3",
                "CODEX_AUTOMATE_GOAL_ID": "42",
                "CODEX_AUTOMATE_AGENT_NAMES": "lead, qa",
                "CODEX_AUTOMATE_STOP_WHEN_IDLE": "1",
                "CODEX_AUTOMATE_REQUIRE_PERSISTENT_DB": "0",
            }
        )

        self.assertEqual(config.database_target, "postgresql://user:pass@example.com/db")
        self.assertEqual(config.workspace_root, self.workspace_root.resolve())
        self.assertEqual(config.poll_seconds, 7.5)
        self.assertEqual(config.max_cycles, 3)
        self.assertEqual(config.goal_id, 42)
        self.assertEqual(config.agent_names, ("lead", "qa"))
        self.assertTrue(config.stop_when_idle)
        self.assertFalse(config.require_persistent_db)

    def test_inspect_rejects_local_sqlite_when_persistent_db_is_required(self) -> None:
        config = WorkerHostConfig(
            database_target=str(self.db_path),
            workspace_root=self.workspace_root,
        )

        with self.assertRaisesRegex(ValueError, "persistent Postgres database"):
            inspect_worker_host(config)

    def test_inspect_accepts_local_sqlite_when_guard_disabled(self) -> None:
        config = WorkerHostConfig(
            database_target=str(self.db_path),
            workspace_root=self.workspace_root,
            require_persistent_db=False,
        )

        summary = inspect_worker_host(config)

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["backend"], "sqlite")
        self.assertEqual(summary["agent_count"], 0)

    def test_inspect_requires_codex_cli_for_codex_exec_agents(self) -> None:
        store = StateStore(str(self.db_path))
        store.initialize()
        store.register_agent("planner", ["planning"])
        config = WorkerHostConfig(
            database_target=str(self.db_path),
            workspace_root=self.workspace_root,
            require_persistent_db=False,
        )

        with patch("codex_automate.worker_host.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "codex.*CLI"):
                inspect_worker_host(config)


if __name__ == "__main__":
    unittest.main()
