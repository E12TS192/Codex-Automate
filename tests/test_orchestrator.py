from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_automate.orchestrator import Orchestrator
from codex_automate.runtime import WorkerRuntime
from codex_automate.simulation import SimulatedWorker
from codex_automate.state import StateStore


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.workspace_root = Path(self.temp_dir.name)
        self.store = StateStore(str(self.db_path))
        self.store.initialize()
        self.orchestrator = Orchestrator(self.store, lease_seconds=120, resolution_capability="orchestrator")
        self.runtime = WorkerRuntime(
            store=self.store,
            workspace_root=str(self.workspace_root),
            orchestrator=self.orchestrator,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_assignments_respect_dependencies(self) -> None:
        self.store.register_agent("lead", ["orchestrator", "planning"])
        self.store.register_agent("builder", ["backend"])

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Dependency test",
                "packages": [
                    {
                        "key": "plan",
                        "title": "Plan",
                        "description": "Planning package",
                        "capability": "planning",
                        "priority": 100
                    },
                    {
                        "key": "build",
                        "title": "Build",
                        "description": "Backend package",
                        "capability": "backend",
                        "priority": 90,
                        "depends_on": ["plan"]
                    },
                ],
            }
        )

        tick_one = self.orchestrator.tick()
        self.assertEqual(len(tick_one["assignments"]), 1)
        self.assertEqual(tick_one["assignments"][0]["agent_name"], "lead")

        SimulatedWorker(self.store, "lead").step()
        tick_two = self.orchestrator.tick()
        self.assertEqual(len(tick_two["assignments"]), 1)
        self.assertEqual(tick_two["assignments"][0]["agent_name"], "builder")

        SimulatedWorker(self.store, "builder").step()
        dashboard = self.orchestrator.dashboard(goal_id)
        self.assertEqual(dashboard["goal"]["status"], "completed")

    def test_blockers_create_resolution_and_requeue_parent(self) -> None:
        self.store.register_agent("lead", ["orchestrator", "planning"])
        self.store.register_agent("qa", ["qa"])

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Blocker test",
                "packages": [
                    {
                        "key": "qa",
                        "title": "QA run",
                        "description": "Exercise blocker path",
                        "capability": "qa",
                        "priority": 100,
                        "metadata": {
                            "block_once": True,
                            "block_reason": "Need rollback decision"
                        }
                    }
                ],
            }
        )

        self.orchestrator.tick()
        first_action = SimulatedWorker(self.store, "qa").step()
        self.assertIn("blocked", first_action)

        tick_two = self.orchestrator.tick()
        self.assertEqual(len(tick_two["resolution_packages"]), 1)
        self.assertEqual(len(tick_two["assignments"]), 1)
        self.assertEqual(tick_two["assignments"][0]["agent_name"], "lead")

        SimulatedWorker(self.store, "lead").step()
        tick_three = self.orchestrator.tick()
        self.assertEqual(len(tick_three["requeued_packages"]), 1)
        self.assertEqual(tick_three["assignments"][0]["agent_name"], "qa")

        SimulatedWorker(self.store, "qa").step()
        dashboard = self.orchestrator.dashboard(goal_id)
        self.assertEqual(dashboard["goal"]["status"], "completed")

    def test_shell_worker_completes_assigned_package(self) -> None:
        command = """python3 - <<'PY'
import json
import os
from pathlib import Path

result = {
    "status": "completed",
    "summary": "shell runner finished the package",
    "artifacts": [],
    "notes": []
}
Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps(result),
    encoding="utf-8",
)
PY"""
        self.store.register_agent(
            "shell-lead",
            ["planning"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": command,
                    "cwd": str(self.workspace_root),
                }
            },
        )

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Shell runner test",
                "packages": [
                    {
                        "title": "Planning package",
                        "description": "Complete one package through the real worker runtime",
                        "capability": "planning",
                        "priority": 100
                    }
                ],
            }
        )

        tick_result = self.orchestrator.tick()
        self.assertEqual(len(tick_result["assignments"]), 1)

        worker_result = self.runtime.run_agent_once("shell-lead")
        self.assertEqual(worker_result["outcome"], "completed")

        dashboard = self.orchestrator.dashboard(goal_id)
        self.assertEqual(dashboard["goal"]["status"], "completed")
        package = self.store.list_packages(goal_id=goal_id)[0]
        self.assertEqual(package["metadata"]["latest_run"]["runner_type"], "shell")

    def test_autopilot_runs_shell_workers_to_completion(self) -> None:
        lead_command = """python3 - <<'PY'
import json
import os
from pathlib import Path

result = {
    "status": "completed",
    "summary": "lead resolved or completed the assigned package",
    "artifacts": [],
    "notes": []
}
Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps(result),
    encoding="utf-8",
)
PY"""
        qa_command = """python3 - <<'PY'
import json
import os
from pathlib import Path

context = json.loads(Path(os.environ["CODEX_AUTOMATE_CONTEXT_FILE"]).read_text(encoding="utf-8"))
marker = Path(os.environ["CODEX_AUTOMATE_WORKSPACE"]) / f"qa-block-{context['package']['id']}.marker"
if marker.exists():
    result = {
        "status": "completed",
        "summary": "qa completed after blocker resolution",
        "artifacts": [],
        "notes": []
    }
else:
    marker.write_text("blocked", encoding="utf-8")
    result = {
        "status": "blocked",
        "summary": "qa blocked the package",
        "blocker_reason": "Need rollback decision",
        "artifacts": [],
        "notes": []
    }
Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps(result),
    encoding="utf-8",
)
PY"""
        self.store.register_agent(
            "lead",
            ["orchestrator", "planning"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": lead_command,
                    "cwd": str(self.workspace_root),
                }
            },
        )
        self.store.register_agent(
            "qa",
            ["qa"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": qa_command,
                    "cwd": str(self.workspace_root),
                }
            },
        )

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Autopilot shell flow",
                "packages": [
                    {
                        "title": "QA package",
                        "description": "Exercise blocker resolution through the real worker runtime",
                        "capability": "qa",
                        "priority": 100
                    }
                ],
            }
        )

        result = self.runtime.run_autopilot(goal_id=goal_id, max_iterations=6)
        self.assertEqual(result["dashboard"]["goal"]["status"], "completed")
        self.assertGreaterEqual(len(result["timeline"]), 3)


if __name__ == "__main__":
    unittest.main()
