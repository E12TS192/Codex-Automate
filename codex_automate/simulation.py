from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from codex_automate.database import is_postgres_target, normalize_sqlite_target
from codex_automate.models import AgentStatus, GoalStatus
from codex_automate.orchestrator import Orchestrator
from codex_automate.state import StateStore


class SimulatedWorker:
    def __init__(self, store: StateStore, name: str) -> None:
        agent = store.get_agent_by_name(name)
        if agent is None:
            raise ValueError(f"Unknown agent '{name}'")
        self.store = store
        self.agent_id = agent["id"]

    def step(self) -> Optional[str]:
        agent = self.store.get_agent(self.agent_id)
        if agent is None:
            return None
        status = AgentStatus.BUSY.value if agent["current_package_id"] else AgentStatus.IDLE.value
        self.store.heartbeat(self.agent_id, status=status)
        package = self.store.get_current_package_for_agent(self.agent_id)
        if package is None:
            return None

        self.store.mark_assignment_active(self.agent_id)
        if package["kind"] == "unblock":
            self.store.complete_current_package(
                self.agent_id,
                summary=f"Resolved blocker for package {package['parent_package_id']}",
            )
            return f"{agent['name']} resolved blocker for package {package['parent_package_id']}"

        metadata = dict(package["metadata"])
        if metadata.get("block_once") and not metadata.get("_block_once_consumed"):
            metadata["_block_once_consumed"] = True
            self.store.update_package_metadata(package["id"], metadata)
            reason = metadata.get("block_reason", "No blocker reason provided.")
            self.store.block_current_package(self.agent_id, reason=reason)
            return f"{agent['name']} blocked '{package['title']}': {reason}"

        summary = metadata.get(
            "success_summary",
            f"{agent['name']} completed '{package['title']}'",
        )
        self.store.complete_current_package(self.agent_id, summary=summary)
        return summary


def build_demo_goal() -> Dict[str, Any]:
    return {
        "title": "Set up an autonomous Codex delivery loop",
        "objective": "Show that an orchestrator can assign work, detect blockers and continue autonomously.",
        "acceptance_criteria": [
            "Work is split into packages with dependencies.",
            "A blocker produces a dedicated unblock package.",
            "The original package is requeued after blocker resolution.",
        ],
        "packages": [
            {
                "key": "operating-model",
                "title": "Define operating model",
                "description": "Describe roles, message contracts and state ownership.",
                "capability": "planning",
                "priority": 100,
                "metadata": {
                    "success_summary": "Operating model documented",
                },
            },
            {
                "key": "state-layer",
                "title": "Implement state layer",
                "description": "Build the persistent state and scheduling backbone.",
                "capability": "backend",
                "priority": 90,
                "depends_on": ["operating-model"],
                "metadata": {
                    "success_summary": "Persistent state layer implemented",
                },
            },
            {
                "key": "qa-flow",
                "title": "Validate blocker recovery",
                "description": "Run the integration validation for blocker handling.",
                "capability": "qa",
                "priority": 80,
                "depends_on": ["state-layer"],
                "metadata": {
                    "block_once": True,
                    "block_reason": "Missing rollback decision for blocked-package recovery",
                    "success_summary": "QA validated blocker recovery flow",
                },
            },
        ],
    }


def run_demo(db_path: str, reset: bool = False, max_steps: int = 12) -> Dict[str, Any]:
    if is_postgres_target(db_path):
        raise ValueError("The local demo only supports SQLite targets.")

    database = Path(normalize_sqlite_target(db_path))
    if reset and database.exists():
        database.unlink()

    store = StateStore(str(database))
    store.initialize()
    orchestrator = Orchestrator(store=store, lease_seconds=300, resolution_capability="orchestrator")

    store.register_agent("lead-orchestrator", ["orchestrator", "planning"])
    store.register_agent("builder", ["backend"])
    store.register_agent("qa-runner", ["qa"])

    goal_id = orchestrator.submit_goal_from_dict(build_demo_goal())

    workers = [
        SimulatedWorker(store, "lead-orchestrator"),
        SimulatedWorker(store, "builder"),
        SimulatedWorker(store, "qa-runner"),
    ]

    timeline: List[Dict[str, Any]] = []
    dashboard: Dict[str, Any] = {}
    for step in range(1, max_steps + 1):
        tick_result = orchestrator.tick()
        worker_actions = [action for action in (worker.step() for worker in workers) if action]
        dashboard = orchestrator.dashboard(goal_id)
        timeline.append(
            {
                "step": step,
                "tick": tick_result,
                "worker_actions": worker_actions,
                "goal_status": dashboard["goal"]["status"] if dashboard["goal"] else None,
            }
        )
        if dashboard["goal"] and dashboard["goal"]["status"] == GoalStatus.COMPLETED.value:
            break

    return {
        "goal_id": goal_id,
        "timeline": timeline,
        "dashboard": dashboard,
    }
