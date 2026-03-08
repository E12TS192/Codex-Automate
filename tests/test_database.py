from __future__ import annotations

import os
import unittest

from codex_automate.database import resolve_database_target
from codex_automate.state import _json_dumps


class DatabaseTargetTests(unittest.TestCase):
    def test_vercel_defaults_to_tmp_sqlite_when_no_database_url_is_set(self) -> None:
        old_vercel = os.environ.get("VERCEL")
        old_codex_db = os.environ.get("CODEX_AUTOMATE_DATABASE_URL")
        old_database_url = os.environ.get("DATABASE_URL")
        old_postgres_url = os.environ.get("POSTGRES_URL")
        try:
            os.environ["VERCEL"] = "1"
            os.environ.pop("CODEX_AUTOMATE_DATABASE_URL", None)
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            self.assertEqual(resolve_database_target(None), "/tmp/codex_automate.sqlite3")
        finally:
            if old_vercel is None:
                os.environ.pop("VERCEL", None)
            else:
                os.environ["VERCEL"] = old_vercel
            if old_codex_db is None:
                os.environ.pop("CODEX_AUTOMATE_DATABASE_URL", None)
            else:
                os.environ["CODEX_AUTOMATE_DATABASE_URL"] = old_codex_db
            if old_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_database_url
            if old_postgres_url is None:
                os.environ.pop("POSTGRES_URL", None)
            else:
                os.environ["POSTGRES_URL"] = old_postgres_url

    def test_json_dump_preserves_empty_list_shape(self) -> None:
        self.assertEqual(_json_dumps([]), "[]")


if __name__ == "__main__":
    unittest.main()
