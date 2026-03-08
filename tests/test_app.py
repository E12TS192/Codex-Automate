from __future__ import annotations

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
        self.temp_dir.cleanup()

    def test_health_and_dashboard(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["backend"], "sqlite")

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("meta", dashboard.json())

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
