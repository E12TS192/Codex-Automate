from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse, urlunparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only when postgres support is missing
    psycopg = None
    dict_row = None


DEFAULT_SQLITE_TARGET = "state/codex_automate.sqlite3"

SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    parent_package_id INTEGER,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    capability TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    kind TEXT NOT NULL DEFAULT 'delivery',
    status TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    dependency_ids TEXT NOT NULL DEFAULT '[]',
    blocker_reason TEXT,
    assignment_id INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(goal_id) REFERENCES goals(id),
    FOREIGN KEY(parent_package_id) REFERENCES work_packages(id)
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    last_heartbeat_at TEXT,
    current_package_id INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(current_package_id) REFERENCES work_packages(id)
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    completed_at TEXT,
    result_summary TEXT,
    FOREIGN KEY(package_id) REFERENCES work_packages(id),
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_goal_status
    ON work_packages(goal_id, status);

CREATE INDEX IF NOT EXISTS idx_assignments_status
    ON assignments(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_events_entity
    ON events(entity_type, entity_id, created_at);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_packages (
    id BIGSERIAL PRIMARY KEY,
    goal_id BIGINT NOT NULL REFERENCES goals(id),
    parent_package_id BIGINT REFERENCES work_packages(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    capability TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    kind TEXT NOT NULL DEFAULT 'delivery',
    status TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    dependency_ids TEXT NOT NULL DEFAULT '[]',
    blocker_reason TEXT,
    assignment_id BIGINT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    last_heartbeat_at TEXT,
    current_package_id BIGINT REFERENCES work_packages(id),
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS assignments (
    id BIGSERIAL PRIMARY KEY,
    package_id BIGINT NOT NULL REFERENCES work_packages(id),
    agent_id BIGINT NOT NULL REFERENCES agents(id),
    status TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    completed_at TEXT,
    result_summary TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_goal_status
    ON work_packages(goal_id, status);

CREATE INDEX IF NOT EXISTS idx_assignments_status
    ON assignments(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_events_entity
    ON events(entity_type, entity_id, created_at);
"""


def resolve_database_target(explicit_target: Optional[str] = None) -> str:
    return (
        explicit_target
        or os.getenv("CODEX_AUTOMATE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or DEFAULT_SQLITE_TARGET
    )


def is_postgres_target(target: str) -> bool:
    return target.startswith("postgres://") or target.startswith("postgresql://")


def is_sqlite_url(target: str) -> bool:
    return target.startswith("sqlite:///")


def normalize_sqlite_target(target: str) -> str:
    if is_sqlite_url(target):
        return target.replace("sqlite:///", "", 1)
    return target


def redact_database_target(target: str) -> str:
    if is_postgres_target(target):
        parsed = urlparse(target)
        netloc = parsed.hostname or ""
        if parsed.username:
            netloc = parsed.username
            if parsed.password:
                netloc += ":***"
            if parsed.hostname:
                netloc += f"@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    if is_sqlite_url(target):
        return target
    return f"sqlite:///{Path(target).resolve()}"


class PostgresCursorWrapper:
    def __init__(self, cursor: Any, lastrowid: Optional[int] = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> Any:
        return self._cursor.fetchall()


class PostgresConnectionWrapper:
    def __init__(self, dsn: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError(
                "Postgres support requires 'psycopg[binary]'. Install project dependencies first."
            )
        self._connection = psycopg.connect(dsn, row_factory=dict_row)

    def __enter__(self) -> "PostgresConnectionWrapper":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._connection.close()

    def _translate_query(self, query: str) -> str:
        return query.replace("?", "%s")

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> PostgresCursorWrapper:
        cursor = self._connection.cursor()
        translated = self._translate_query(query)
        is_insert = translated.lstrip().upper().startswith("INSERT INTO")
        if is_insert and "RETURNING" not in translated.upper():
            translated = translated.rstrip().rstrip(";") + " RETURNING id"
        cursor.execute(translated, params or ())
        lastrowid = None
        if is_insert:
            row = cursor.fetchone()
            if row:
                lastrowid = int(row["id"])
        return PostgresCursorWrapper(cursor, lastrowid=lastrowid)

    def executescript(self, script: str) -> None:
        statements = [statement.strip() for statement in script.split(";") if statement.strip()]
        for statement in statements:
            self.execute(statement)


def ensure_sqlite_parent(target: str) -> Path:
    path = Path(normalize_sqlite_target(target))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
