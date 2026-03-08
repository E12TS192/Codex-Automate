from __future__ import annotations

import threading
import tempfile
import time
import unittest
from pathlib import Path

from codex_automate.orchestrator import Orchestrator
from codex_automate.runtime import RunnerTimeoutError, WorkerRuntime
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

    def test_goal_without_packages_creates_default_discovery_pipeline(self) -> None:
        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Natural language goal",
                "objective": "Build a small internal tool from a free-form request.",
            }
        )

        packages = self.store.list_packages(goal_id=goal_id)
        self.assertEqual(
            [package["metadata"]["stage"] for package in packages],
            ["mvp_scope", "integration_feasibility", "risk_review", "architecture", "breakdown"],
        )
        self.assertEqual(packages[1]["dependency_ids"], [packages[0]["id"]])
        self.assertEqual(packages[2]["dependency_ids"], [packages[1]["id"]])
        self.assertEqual(packages[3]["dependency_ids"], [packages[2]["id"]])
        self.assertEqual(packages[4]["dependency_ids"], [packages[3]["id"]])

    def test_delivery_capability_aliases_unlock_generated_work(self) -> None:
        planner_id = self.store.register_agent("planner", ["planning"])
        delivery_id = self.store.register_agent("delivery", ["backend", "api", "docs"])

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Generated delivery work",
                "packages": [
                    {
                        "key": "plan",
                        "title": "Planning",
                        "description": "Seed completed planning output",
                        "capability": "planning",
                        "priority": 100,
                    },
                    {
                        "key": "build",
                        "title": "Implementation package",
                        "description": "Needs a delivery agent",
                        "capability": "implementation",
                        "priority": 90,
                        "depends_on": ["plan"],
                    },
                    {
                        "key": "docs",
                        "title": "Documentation package",
                        "description": "Needs docs capability alias",
                        "capability": "documentation",
                        "priority": 80,
                        "depends_on": ["build"],
                    },
                ],
            }
        )

        tick_one = self.orchestrator.tick()
        self.assertEqual(tick_one["assignments"][0]["agent_name"], "planner")
        self.store.complete_current_package(planner_id, summary="planned")

        tick_two = self.orchestrator.tick()
        self.assertEqual(tick_two["assignments"][0]["agent_name"], "delivery")
        self.assertEqual(tick_two["assignments"][0]["package_title"], "Implementation package")
        self.store.complete_current_package(delivery_id, summary="implemented")

        tick_three = self.orchestrator.tick()
        self.assertEqual(tick_three["assignments"][0]["agent_name"], "delivery")
        self.assertEqual(tick_three["assignments"][0]["package_title"], "Documentation package")

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

    def test_blocked_resolution_package_does_not_spawn_recursive_resolution(self) -> None:
        lead_id = self.store.register_agent("lead", ["orchestrator", "planning"])
        self.store.register_agent("qa", ["qa"])

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Resolution recursion guard",
                "packages": [
                    {
                        "key": "qa",
                        "title": "QA run",
                        "description": "Exercise blocker resolution recursion guard",
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
        SimulatedWorker(self.store, "qa").step()

        first_resolution_tick = self.orchestrator.tick()
        self.assertEqual(len(first_resolution_tick["resolution_packages"]), 1)

        self.store.block_current_package(lead_id, "Resolution package timed out")
        second_resolution_tick = self.orchestrator.tick()
        self.assertEqual(second_resolution_tick["resolution_packages"], [])

        packages = self.store.list_packages(goal_id=goal_id)
        resolution_packages = [package for package in packages if package["kind"] == "unblock"]
        self.assertEqual(len(resolution_packages), 1)
        self.assertEqual(resolution_packages[0]["status"], "blocked")

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

    def test_service_loop_runs_until_idle_or_complete(self) -> None:
        command = """python3 - <<'PY'
import json
import os
from pathlib import Path

Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps({
        "status": "completed",
        "summary": "service worker completed the package",
        "blocker_reason": "",
        "artifacts": [],
        "notes": []
    }),
    encoding="utf-8",
)
PY"""
        self.store.register_agent(
            "service-planner",
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
                "title": "Service loop test",
                "packages": [
                    {
                        "title": "Single service package",
                        "description": "Complete through the poll loop",
                        "capability": "planning",
                        "priority": 100,
                    }
                ],
            }
        )

        result = self.runtime.run_service(
            goal_id=goal_id,
            poll_seconds=0,
            max_cycles=3,
            agent_names=["service-planner"],
            stop_when_idle=True,
        )
        self.assertEqual(result["dashboard"]["goal"]["status"], "completed")
        self.assertGreaterEqual(len(result["cycles"]), 1)

    def test_long_running_worker_renews_lease_until_completion(self) -> None:
        orchestrator = Orchestrator(self.store, lease_seconds=1, resolution_capability="orchestrator")
        runtime = WorkerRuntime(
            store=self.store,
            workspace_root=str(self.workspace_root),
            orchestrator=orchestrator,
        )
        command = """python3 - <<'PY'
import json
import os
import time
from pathlib import Path

time.sleep(2.0)
Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps({
        "status": "completed",
        "summary": "slow worker completed after renewing its lease",
        "blocker_reason": "",
        "artifacts": [],
        "notes": []
    }),
    encoding="utf-8",
)
PY"""
        self.store.register_agent(
            "slow-planner",
            ["planning"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": command,
                    "cwd": str(self.workspace_root),
                    "timeout_seconds": 5,
                    "heartbeat_interval_seconds": 0.2,
                }
            },
        )
        goal_id = orchestrator.submit_goal_from_dict(
            {
                "title": "Lease renewal test",
                "packages": [
                    {
                        "title": "Slow package",
                        "description": "Run long enough that lease renewal matters",
                        "capability": "planning",
                        "priority": 100,
                    }
                ],
            }
        )

        orchestrator.tick()
        result_holder: dict[str, object] = {}
        worker_thread = threading.Thread(
            target=lambda: result_holder.setdefault("result", runtime.run_agent_once("slow-planner")),
            daemon=True,
        )
        worker_thread.start()
        time.sleep(1.4)

        expired = self.store.expire_assignments()
        worker_thread.join(timeout=6)

        self.assertEqual(expired, [])
        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(result_holder["result"]["outcome"], "completed")
        dashboard = orchestrator.dashboard(goal_id)
        self.assertEqual(dashboard["goal"]["status"], "completed")

    def test_worker_timeout_marks_package_blocked(self) -> None:
        orchestrator = Orchestrator(self.store, lease_seconds=10, resolution_capability="orchestrator")
        runtime = WorkerRuntime(
            store=self.store,
            workspace_root=str(self.workspace_root),
            orchestrator=orchestrator,
        )
        command = """python3 - <<'PY'
import time

time.sleep(2.0)
PY"""
        self.store.register_agent(
            "timeout-qa",
            ["qa"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": command,
                    "cwd": str(self.workspace_root),
                    "timeout_seconds": 0.5,
                    "heartbeat_interval_seconds": 0.1,
                }
            },
        )
        goal_id = orchestrator.submit_goal_from_dict(
            {
                "title": "Timeout handling test",
                "packages": [
                    {
                        "title": "Hung package",
                        "description": "Block when the runner exceeds its timeout",
                        "capability": "qa",
                        "priority": 100,
                    }
                ],
            }
        )

        orchestrator.tick()
        result = runtime.run_agent_once("timeout-qa")

        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["package_title"], "Hung package")
        package = self.store.list_packages(goal_id=goal_id)[0]
        self.assertEqual(package["status"], "blocked")
        self.assertIn("exceeded 0.5s", package["blocker_reason"])
        self.assertEqual(package["metadata"]["latest_run"]["status"], "blocked")
        dashboard = orchestrator.dashboard(goal_id)
        self.assertEqual(dashboard["goal"]["status"], "blocked")

    def test_monitored_process_accepts_stdin_without_repeated_communicate(self) -> None:
        completed = self.runtime._run_monitored_process(
            command=["/bin/cat"],
            cwd=self.workspace_root,
            env={},
            input_text="worker stdin smoke\n",
            shell=False,
            agent_id=self.store.register_agent("stdin-smoke", ["planning"]),
            runner={"timeout_seconds": 2, "heartbeat_interval_seconds": 0.1},
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "worker stdin smoke\n")
        self.assertEqual(completed.stderr, "")

    def test_monitored_process_timeout_with_stdin_raises_runner_timeout(self) -> None:
        with self.assertRaises(RunnerTimeoutError):
            self.runtime._run_monitored_process(
                command=["python3", "-c", "import time; time.sleep(1)"],
                cwd=self.workspace_root,
                env={},
                input_text="worker stdin smoke\n",
                shell=False,
                agent_id=self.store.register_agent("stdin-timeout", ["planning"]),
                runner={"timeout_seconds": 0.2, "heartbeat_interval_seconds": 0.1},
            )

    def test_discovery_pipeline_can_generate_follow_on_packages(self) -> None:
        planning_command = """python3 - <<'PY'
import json
import os
from pathlib import Path

context = json.loads(Path(os.environ["CODEX_AUTOMATE_CONTEXT_FILE"]).read_text(encoding="utf-8"))
stage = context["package"]["metadata"].get("stage")
result = {
    "status": "completed",
    "summary": f"completed planning stage {stage}",
    "blocker_reason": "",
    "artifacts": [],
    "notes": [],
    "new_packages": [],
    "stage_output": {}
}
if stage == "mvp_scope":
    result["summary"] = "the first release is viable if the MVP stays focused on admin search, visibility and controlled offboarding actions"
    result["stage_output"] = {
        "verdict": "go",
        "key_points": [
            "The MVP can stay narrow around search, account visibility and guided offboarding.",
            "A planning-first approach reduces downstream rework."
        ],
        "risks": [
            "Scope will sprawl if license optimization and every SaaS edge case are included immediately."
        ],
        "open_questions": [
            "Which admin roles need access to the first usable release?"
        ]
    }
elif stage == "integration_feasibility":
    result["summary"] = "a first wave of identity and SaaS integrations is feasible if the MVP prioritizes a few core systems"
    result["stage_output"] = {
        "verdict": "conditional",
        "key_points": [
            "The MVP should start with a small set of systems such as the identity provider, productivity suite and one collaboration platform.",
            "API breadth varies, so connector scope must be intentionally prioritized."
        ],
        "risks": [
            "Advanced deprovisioning and data transfer flows differ significantly between vendors."
        ],
        "open_questions": [
            "Which systems are mandatory for the first release?"
        ]
    }
elif stage == "risk_review":
    result["summary"] = "the project should proceed with constraints around approvals, auditability and destructive actions"
    result["stage_output"] = {
        "verdict": "conditional",
        "key_points": [
            "The concept is viable if destructive actions are gated and fully auditable.",
            "A dry-run mode is necessary before enabling real offboarding changes."
        ],
        "risks": [
            "Offboarding workflows can create high-impact mistakes without approvals and rollback guidance."
        ],
        "open_questions": [
            "Which approval policy applies to account disablement and license removal?"
        ]
    }
elif stage == "architecture":
    result["summary"] = "recommend a thin control plane with staged worker execution"
    result["stage_output"] = {
        "components": [
            "Protected dashboard for goal intake and status.",
            "Worker runtime that executes packages and writes structured results."
        ],
        "decisions": [
            "Keep discovery separate from delivery work.",
            "Use small capability-based packages instead of one large execution plan."
        ],
        "delivery_sequence": [
            "Lock the discovery flow and prompts.",
            "Generate implementation and QA packages from the breakdown stage."
        ],
        "validation_strategy": [
            "Run automated tests after prompt and runtime changes.",
            "Confirm generated packages preserve dependencies."
        ],
        "handoff": "Break the architecture into executable backend and QA work packages."
    }
elif stage == "breakdown":
    result["summary"] = "created backend and qa follow-on packages"
    result["new_packages"] = [
        {
            "key": "backend_impl",
            "title": "Implement core feature",
            "description": "Build the core backend implementation for the requested project.",
            "capability": "backend",
            "priority": 90,
            "kind": "delivery"
        },
        {
            "title": "Run QA verification",
            "description": "Verify the generated implementation package.",
            "capability": "qa",
            "priority": 80,
            "kind": "delivery",
            "depends_on": ["backend_impl"]
        }
    ]
    result["stage_output"] = {
        "generated_package_titles": [
            "Implement core feature",
            "Run QA verification"
        ],
        "generated_package_count": 2,
        "handoff": "Backend builds the feature first, then QA verifies the result."
    }
Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps(result),
    encoding="utf-8",
)
PY"""
        delivery_command = """python3 - <<'PY'
import json
import os
from pathlib import Path

context = json.loads(Path(os.environ["CODEX_AUTOMATE_CONTEXT_FILE"]).read_text(encoding="utf-8"))
Path(os.environ["CODEX_AUTOMATE_RESULT_FILE"]).write_text(
    json.dumps({
        "status": "completed",
        "summary": f"completed {context['package']['capability']} package",
        "blocker_reason": "",
        "artifacts": [],
        "notes": [],
        "new_packages": []
    }),
    encoding="utf-8",
)
PY"""
        self.store.register_agent(
            "planner",
            ["planning"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": planning_command,
                    "cwd": str(self.workspace_root),
                }
            },
        )
        self.store.register_agent(
            "builder",
            ["backend"],
            metadata={
                "runner": {
                    "type": "shell",
                    "command": delivery_command,
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
                    "command": delivery_command,
                    "cwd": str(self.workspace_root),
                }
            },
        )

        goal_id = self.orchestrator.submit_goal_from_dict(
            {
                "title": "Staged planning flow",
                "objective": "Start from a free-form goal and generate implementation work.",
            }
        )

        result = self.runtime.run_autopilot(goal_id=goal_id, max_iterations=12)
        self.assertEqual(result["dashboard"]["goal"]["status"], "completed")

        packages = self.store.list_packages(goal_id=goal_id)
        self.assertEqual(len(packages), 7)
        stage_packages = {
            package["metadata"].get("stage"): package
            for package in packages
            if package["metadata"].get("stage")
        }
        self.assertEqual(stage_packages["mvp_scope"]["metadata"]["stage_output"]["verdict"], "go")
        self.assertEqual(stage_packages["integration_feasibility"]["metadata"]["stage_output"]["verdict"], "conditional")
        self.assertEqual(stage_packages["risk_review"]["metadata"]["stage_output"]["verdict"], "conditional")
        self.assertIn("Break the architecture", stage_packages["architecture"]["metadata"]["stage_output"]["handoff"])
        self.assertEqual(stage_packages["breakdown"]["metadata"]["stage_output"]["generated_package_count"], 2)
        generated = [package for package in packages if package["metadata"].get("generated_by_package_id")]
        self.assertEqual(len(generated), 2)
        breakdown = next(package for package in packages if package["metadata"].get("stage") == "breakdown")
        self.assertEqual(
            breakdown["metadata"]["latest_run"]["stage_output"]["generated_package_titles"],
            ["Implement core feature", "Run QA verification"],
        )
        for package in generated:
            self.assertEqual(package["parent_package_id"], breakdown["id"])
            self.assertIn(breakdown["id"], package["dependency_ids"])


if __name__ == "__main__":
    unittest.main()
