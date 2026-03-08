from __future__ import annotations

from typing import Any, Dict, List, Optional

from codex_automate.models import AgentStatus, GoalInput, WorkPackageInput
from codex_automate.state import StateStore


class Orchestrator:
    def __init__(
        self,
        store: StateStore,
        lease_seconds: int = 900,
        resolution_capability: str = "orchestrator",
    ) -> None:
        self.store = store
        self.lease_seconds = lease_seconds
        self.resolution_capability = resolution_capability

    def submit_goal(self, goal_input: GoalInput) -> int:
        goal_id = self.store.create_goal(
            title=goal_input.title,
            objective=goal_input.objective,
            acceptance_criteria=goal_input.acceptance_criteria,
        )
        key_to_package_id: Dict[str, int] = {}
        dependency_links: List[tuple[int, List[str]]] = []
        for package in goal_input.packages:
            package_id = self.store.create_work_package(
                goal_id=goal_id,
                title=package.title,
                description=package.description,
                capability=package.capability,
                priority=package.priority,
                kind=package.kind,
                acceptance_criteria=package.acceptance_criteria,
                metadata=package.metadata,
            )
            if package.key:
                if package.key in key_to_package_id:
                    raise ValueError(f"Duplicate package key: {package.key}")
                key_to_package_id[package.key] = package_id
            dependency_links.append((package_id, list(package.depends_on)))

        for package_id, dependency_keys in dependency_links:
            dependency_ids: List[int] = []
            for dependency_key in dependency_keys:
                if dependency_key not in key_to_package_id:
                    raise ValueError(f"Unknown dependency key: {dependency_key}")
                dependency_ids.append(key_to_package_id[dependency_key])
            if dependency_ids:
                self.store.update_package_dependencies(package_id, dependency_ids)

        self.store.refresh_goal_status(goal_id)
        return goal_id

    def submit_goal_from_dict(self, payload: Dict[str, Any]) -> int:
        packages = [
            WorkPackageInput(
                title=item["title"],
                description=item["description"],
                capability=item["capability"],
                priority=int(item.get("priority", 50)),
                kind=item.get("kind", "delivery"),
                key=item.get("key"),
                depends_on=list(item.get("depends_on", [])),
                acceptance_criteria=list(item.get("acceptance_criteria", [])),
                metadata=dict(item.get("metadata", {})),
            )
            for item in payload.get("packages", [])
        ]
        if not packages:
            raise ValueError("A goal requires at least one package.")
        goal_input = GoalInput(
            title=payload["title"],
            objective=payload.get("objective", payload["title"]),
            acceptance_criteria=list(payload.get("acceptance_criteria", [])),
            packages=packages,
        )
        return self.submit_goal(goal_input)

    def _requeue_resolved_blockers(self) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        for blocked in self.store.find_blocked_packages_ready_for_requeue():
            self.store.requeue_package(
                blocked["id"],
                reason="Resolution package completed",
            )
            actions.append(
                {
                    "type": "requeued",
                    "package_id": blocked["id"],
                    "title": blocked["title"],
                }
            )
        return actions

    def _create_resolution_packages(self) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        for blocked in self.store.find_blocked_packages_without_resolution():
            resolution_id = self.store.create_resolution_package(
                blocked["id"],
                capability=self.resolution_capability,
            )
            actions.append(
                {
                    "type": "resolution_created",
                    "blocked_package_id": blocked["id"],
                    "resolution_package_id": resolution_id,
                }
            )
        return actions

    def _assign_pending_work(self) -> List[Dict[str, Any]]:
        assignments: List[Dict[str, Any]] = []
        for agent in self.store.list_agents():
            if agent["current_package_id"] is not None:
                continue
            if agent["status"] not in (AgentStatus.IDLE.value, AgentStatus.DEGRADED.value):
                continue
            package = self.store.find_assignable_package(agent["capabilities"])
            if package is None:
                continue
            self.store.assign_package(package["id"], agent["id"], lease_seconds=self.lease_seconds)
            assignments.append(
                {
                    "agent_id": agent["id"],
                    "agent_name": agent["name"],
                    "package_id": package["id"],
                    "package_title": package["title"],
                }
            )
        return assignments

    def tick(self) -> Dict[str, Any]:
        expired = self.store.expire_assignments()
        requeued = self._requeue_resolved_blockers()
        resolutions = self._create_resolution_packages()
        assignments = self._assign_pending_work()
        for goal in self.store.list_goals():
            self.store.refresh_goal_status(goal["id"])
        return {
            "expired_assignments": expired,
            "requeued_packages": requeued,
            "resolution_packages": resolutions,
            "assignments": assignments,
        }

    def dashboard(self, goal_id: Optional[int] = None) -> Dict[str, Any]:
        goals = self.store.list_goals()
        selected_goal = None
        if goal_id is not None:
            selected_goal = self.store.get_goal(goal_id)
        elif goals:
            selected_goal = goals[-1]
            goal_id = selected_goal["id"]
        return {
            "goal": selected_goal,
            "goals": goals,
            "packages": self.store.list_packages(goal_id=goal_id) if goal_id is not None else [],
            "agents": self.store.list_agents(),
            "events": self.store.get_recent_events(),
        }
