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
        self.assertEqual(len(payload["packages"]), 3)

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
