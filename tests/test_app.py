from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import app, get_store


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "api.sqlite3"
        os.environ["CODEX_AUTOMATE_DATABASE_URL"] = str(self.database_path)
        get_store.cache_clear()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        get_store.cache_clear()
        os.environ.pop("CODEX_AUTOMATE_DATABASE_URL", None)
        os.environ.pop("CODEX_AUTOMATE_REQUIRE_AUTH", None)
        os.environ.pop("CODEX_AUTOMATE_AUTH_USERNAME", None)
        os.environ.pop("CODEX_AUTOMATE_AUTH_PASSWORD", None)
        os.environ.pop("VERCEL", None)
        self.temp_dir.cleanup()

    def _basic_auth_header(self, username: str, password: str) -> dict[str, str]:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def test_health_and_dashboard(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["backend"], "sqlite")

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("goal", dashboard.json())

    def test_goal_submission_via_api(self) -> None:
        response = self.client.post(
            "/api/goals",
            json={
                "title": "API goal",
                "packages": [
                    {
                        "title": "Plan via API",
                        "description": "Create a package through the HTTP layer",
                        "capability": "planning",
                        "priority": 100,
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        goal_id = response.json()["goal_id"]

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        payload = dashboard.json()
        self.assertEqual(payload["goal"]["id"], goal_id)
        self.assertEqual(len(payload["packages"]), 1)

    def test_goal_submission_without_packages_creates_discovery_pipeline(self) -> None:
        response = self.client.post(
            "/api/goals",
            json={
                "title": "Free-form API goal",
                "objective": "Turn a user request into staged planning work.",
            },
        )
        self.assertEqual(response.status_code, 200)
        goal_id = response.json()["goal_id"]

        dashboard = self.client.get("/api/dashboard", params={"goal_id": goal_id})
        self.assertEqual(dashboard.status_code, 200)
        payload = dashboard.json()
        self.assertEqual(payload["goal"]["id"], goal_id)
        self.assertEqual(len(payload["packages"]), 5)

    def test_manual_package_note_budget_and_requeue_endpoints(self) -> None:
        goal_response = self.client.post(
            "/api/goals",
            json={
                "title": "Operator goal",
                "packages": [
                    {
                        "title": "Blocked package",
                        "description": "Manual operator intervention target",
                        "capability": "planning",
                        "priority": 100,
                    }
                ],
            },
        )
        self.assertEqual(goal_response.status_code, 200)
        goal_id = goal_response.json()["goal_id"]

        package_response = self.client.post(
            f"/api/goals/{goal_id}/packages",
            json={
                "title": "Manual follow-up",
                "description": "Created from the dashboard",
                "capability": "implementation",
                "priority": 77,
                "kind": "implementation",
                "acceptance_criteria": ["One clear deliverable"],
                "dependency_ids": [],
                "preferred_agent_name": "delivery-generalist",
            },
        )
        self.assertEqual(package_response.status_code, 200)
        self.assertEqual(package_response.json()["package"]["metadata"]["preferred_agent_name"], "delivery-generalist")

        note_response = self.client.post(
            "/api/notes",
            json={
                "goal_id": goal_id,
                "kind": "feedback",
                "title": "Tighten the package scope",
                "body": "Please split the package before the next run.",
            },
        )
        self.assertEqual(note_response.status_code, 200)
        note_id = note_response.json()["note_id"]

        resolve_response = self.client.post(f"/api/notes/{note_id}/resolve")
        self.assertEqual(resolve_response.status_code, 200)

        budget_response = self.client.put(
            "/api/token-budgets",
            json={
                "scope_type": "goal",
                "scope_id": goal_id,
                "total_limit": 5000,
                "enabled": True,
            },
        )
        self.assertEqual(budget_response.status_code, 200)
        self.assertEqual(budget_response.json()["budget"]["total_limit"], 5000)

        store = get_store()
        planner_id = store.register_agent("temp", ["planning"])
        blocked_package = store.list_packages(goal_id=goal_id)[0]
        store.assign_package(blocked_package["id"], planner_id)
        store.block_current_package(planner_id, "Need operator retry")
        requeue_response = self.client.post(
            f"/api/packages/{blocked_package['id']}/requeue",
            json={"reason": "Pre-flight retry"},
        )
        self.assertEqual(requeue_response.status_code, 200)
        self.assertEqual(requeue_response.json()["package"]["status"], "pending")

        dashboard = self.client.get("/api/dashboard", params={"goal_id": goal_id})
        self.assertEqual(dashboard.status_code, 200)
        payload = dashboard.json()
        self.assertIn("operator_notes", payload)
        self.assertIn("token_usage", payload)
        self.assertTrue(any(item["scope_type"] == "goal" for item in payload["token_usage"]["budgets"]))

    def test_dashboard_requires_auth_when_enabled(self) -> None:
        os.environ["CODEX_AUTOMATE_REQUIRE_AUTH"] = "1"
        os.environ["CODEX_AUTOMATE_AUTH_USERNAME"] = "alex"
        os.environ["CODEX_AUTOMATE_AUTH_PASSWORD"] = "secret"

        unauthenticated = self.client.get("/api/dashboard")
        self.assertEqual(unauthenticated.status_code, 401)

        authenticated = self.client.get(
            "/api/dashboard",
            headers=self._basic_auth_header("alex", "secret"),
        )
        self.assertEqual(authenticated.status_code, 200)

    def test_auth_fails_closed_when_required_but_not_configured(self) -> None:
        os.environ["CODEX_AUTOMATE_REQUIRE_AUTH"] = "1"

        response = self.client.get("/")
        self.assertEqual(response.status_code, 503)

    def test_health_stays_public_when_auth_is_enabled(self) -> None:
        os.environ["CODEX_AUTOMATE_REQUIRE_AUTH"] = "1"
        os.environ["CODEX_AUTOMATE_AUTH_USERNAME"] = "alex"
        os.environ["CODEX_AUTOMATE_AUTH_PASSWORD"] = "secret"

        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
