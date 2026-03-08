from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from codex_automate.database import (
    POSTGRES_SCHEMA,
    SQLITE_SCHEMA,
    PostgresConnectionWrapper,
    ensure_sqlite_parent,
    is_postgres_target,
    normalize_sqlite_target,
)
from codex_automate.models import (
    AgentStatus,
    AssignmentStatus,
    EventType,
    GoalStatus,
    PackageStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _as_timestamp(value: Optional[datetime] = None) -> str:
    return (value or _utcnow()).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps({} if value is None else value, ensure_ascii=True, sort_keys=True)


def _json_loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class StateStore:
    def __init__(self, db_path: str = "state/codex_automate.sqlite3") -> None:
        self.database_target = db_path
        self.backend = "postgres" if is_postgres_target(db_path) else "sqlite"
        self.db_path = None
        if self.backend == "sqlite":
            self.db_path = ensure_sqlite_parent(normalize_sqlite_target(db_path))

    def connect(self) -> Any:
        if self.backend == "postgres":
            return PostgresConnectionWrapper(self.database_target)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(POSTGRES_SCHEMA if self.backend == "postgres" else SQLITE_SCHEMA)

    def _record_event(
        self,
        conn: Any,
        entity_type: str,
        entity_id: int,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (entity_type, entity_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, event_type, _json_dumps(payload or {}), _as_timestamp()),
        )

    def _decode_goal(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "objective": row["objective"],
            "acceptance_criteria": _json_loads(row["acceptance_criteria"], []),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _decode_package(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "goal_id": row["goal_id"],
            "parent_package_id": row["parent_package_id"],
            "title": row["title"],
            "description": row["description"],
            "capability": row["capability"],
            "priority": row["priority"],
            "kind": row["kind"],
            "status": row["status"],
            "acceptance_criteria": _json_loads(row["acceptance_criteria"], []),
            "dependency_ids": _json_loads(row["dependency_ids"], []),
            "blocker_reason": row["blocker_reason"],
            "assignment_id": row["assignment_id"],
            "metadata": _json_loads(row["metadata"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _decode_agent(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "capabilities": _json_loads(row["capabilities"], []),
            "status": row["status"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "current_package_id": row["current_package_id"],
            "metadata": _json_loads(row["metadata"], {}),
        }

    def _decode_assignment(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "package_id": row["package_id"],
            "agent_id": row["agent_id"],
            "status": row["status"],
            "assigned_at": row["assigned_at"],
            "lease_expires_at": row["lease_expires_at"],
            "completed_at": row["completed_at"],
            "result_summary": row["result_summary"],
        }

    def _decode_event(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "event_type": row["event_type"],
            "payload": _json_loads(row["payload"], {}),
            "created_at": row["created_at"],
        }

    def create_goal(
        self,
        title: str,
        objective: str,
        acceptance_criteria: Optional[Sequence[str]] = None,
    ) -> int:
        now = _as_timestamp()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO goals (title, objective, acceptance_criteria, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    objective,
                    _json_dumps(list(acceptance_criteria or [])),
                    GoalStatus.NEW.value,
                    now,
                    now,
                ),
            )
            goal_id = int(cursor.lastrowid)
            self._record_event(
                conn,
                "goal",
                goal_id,
                EventType.GOAL_CREATED.value,
                {"title": title},
            )
            return goal_id

    def create_work_package(
        self,
        goal_id: int,
        title: str,
        description: str,
        capability: str,
        priority: int = 50,
        kind: str = "delivery",
        acceptance_criteria: Optional[Sequence[str]] = None,
        dependency_ids: Optional[Sequence[int]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        parent_package_id: Optional[int] = None,
    ) -> int:
        now = _as_timestamp()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO work_packages (
                    goal_id,
                    parent_package_id,
                    title,
                    description,
                    capability,
                    priority,
                    kind,
                    status,
                    acceptance_criteria,
                    dependency_ids,
                    metadata,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    parent_package_id,
                    title,
                    description,
                    capability,
                    priority,
                    kind,
                    PackageStatus.PENDING.value,
                    _json_dumps(list(acceptance_criteria or [])),
                    _json_dumps(list(dependency_ids or [])),
                    _json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            package_id = int(cursor.lastrowid)
            self._record_event(
                conn,
                "work_package",
                package_id,
                EventType.PACKAGE_CREATED.value,
                {
                    "goal_id": goal_id,
                    "title": title,
                    "capability": capability,
                    "kind": kind,
                    "parent_package_id": parent_package_id,
                },
            )
            self._refresh_goal_status(conn, goal_id)
            return package_id

    def update_package_dependencies(self, package_id: int, dependency_ids: Sequence[int]) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT goal_id FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown package {package_id}")
            conn.execute(
                """
                UPDATE work_packages
                SET dependency_ids = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dumps(list(dependency_ids)), _as_timestamp(), package_id),
            )
            self._refresh_goal_status(conn, int(row["goal_id"]))

    def update_package_metadata(self, package_id: int, metadata: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE work_packages
                SET metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dumps(metadata), _as_timestamp(), package_id),
            )

    def register_agent(
        self,
        name: str,
        capabilities: Sequence[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM agents WHERE name = ?",
                (name,),
            ).fetchone()
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO agents (name, capabilities, status, last_heartbeat_at, metadata)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        _json_dumps(list(capabilities)),
                        AgentStatus.IDLE.value,
                        _as_timestamp(),
                        _json_dumps(metadata or {}),
                    ),
                )
                agent_id = int(cursor.lastrowid)
            else:
                agent_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE agents
                    SET capabilities = ?, metadata = ?, last_heartbeat_at = COALESCE(last_heartbeat_at, ?)
                    WHERE id = ?
                    """,
                    (
                        _json_dumps(list(capabilities)),
                        _json_dumps(metadata or {}),
                        _as_timestamp(),
                        agent_id,
                    ),
                )
            self._record_event(
                conn,
                "agent",
                agent_id,
                EventType.AGENT_REGISTERED.value,
                {"name": name, "capabilities": list(capabilities)},
            )
            return agent_id

    def heartbeat(
        self,
        agent_id: int,
        status: Optional[str] = None,
        note: Optional[str] = None,
        lease_seconds: Optional[int] = None,
    ) -> None:
        with self.connect() as conn:
            agent = conn.execute(
                "SELECT current_package_id, status FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if agent is None:
                raise ValueError(f"Unknown agent {agent_id}")
            now = _utcnow()
            new_status = status or agent["status"]
            conn.execute(
                """
                UPDATE agents
                SET last_heartbeat_at = ?, status = ?
                WHERE id = ?
                """,
                (_as_timestamp(now), new_status, agent_id),
            )
            payload: Dict[str, Any] = {"status": new_status, "note": note}
            if agent["current_package_id"] is not None and lease_seconds is not None:
                assignment = conn.execute(
                    """
                    SELECT id
                    FROM assignments
                    WHERE package_id = ? AND agent_id = ? AND status IN (?, ?)
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        int(agent["current_package_id"]),
                        agent_id,
                        AssignmentStatus.ASSIGNED.value,
                        AssignmentStatus.ACTIVE.value,
                    ),
                ).fetchone()
                if assignment is not None:
                    lease_expires_at = _as_timestamp(now + timedelta(seconds=lease_seconds))
                    conn.execute(
                        "UPDATE assignments SET lease_expires_at = ? WHERE id = ?",
                        (lease_expires_at, int(assignment["id"])),
                    )
                    payload["lease_expires_at"] = lease_expires_at
            self._record_event(
                conn,
                "agent",
                agent_id,
                EventType.AGENT_HEARTBEAT.value,
                payload,
            )

    def list_goals(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM goals ORDER BY id ASC"
            ).fetchall()
            return [self._decode_goal(row) for row in rows]

    def get_goal(self, goal_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            return self._decode_goal(row) if row else None

    def list_packages(
        self,
        goal_id: Optional[int] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM work_packages"
        clauses: List[str] = []
        params: List[Any] = []
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if statuses:
            status_values = list(statuses)
            placeholders = ", ".join("?" for _ in status_values)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_values)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority DESC, id ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._decode_package(row) for row in rows]

    def list_child_packages(
        self,
        parent_package_id: int,
        statuses: Optional[Iterable[str]] = None,
        kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM work_packages WHERE parent_package_id = ?"
        params: List[Any] = [parent_package_id]
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        if statuses:
            status_values = list(statuses)
            placeholders = ", ".join("?" for _ in status_values)
            query += f" AND status IN ({placeholders})"
            params.extend(status_values)
        query += " ORDER BY id ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._decode_package(row) for row in rows]

    def get_package(self, package_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            return self._decode_package(row) if row else None

    def list_agents(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY id ASC").fetchall()
            return [self._decode_agent(row) for row in rows]

    def get_agent(self, agent_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return self._decode_agent(row) if row else None

    def get_agent_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
            return self._decode_agent(row) if row else None

    def get_current_package_for_agent(self, agent_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT wp.*
                FROM agents a
                JOIN work_packages wp ON wp.id = a.current_package_id
                WHERE a.id = ?
                """,
                (agent_id,),
            ).fetchone()
            return self._decode_package(row) if row else None

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._decode_event(row) for row in rows]

    def _dependencies_ready(self, conn: Any, dependency_ids: Sequence[int]) -> bool:
        for dependency_id in dependency_ids:
            row = conn.execute(
                "SELECT status FROM work_packages WHERE id = ?",
                (dependency_id,),
            ).fetchone()
            if row is None or row["status"] != PackageStatus.COMPLETED.value:
                return False
        return True

    def _agent_supports_capability(
        self, capabilities: Sequence[str], required_capability: str
    ) -> bool:
        capability_set = set(capabilities)
        aliases = {
            "implementation": {
                "implementation",
                "backend",
                "frontend",
                "api",
                "integration",
                "deployment",
                "security",
                "automation",
            },
            "documentation": {"documentation", "docs"},
        }
        required_set = aliases.get(required_capability, {required_capability})
        return (
            bool(capability_set & required_set)
            or "*" in capability_set
            or "generalist" in capability_set
        )

    def find_assignable_package(self, capabilities: Sequence[str]) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM work_packages
                WHERE status = ?
                ORDER BY priority DESC, id ASC
                """,
                (PackageStatus.PENDING.value,),
            ).fetchall()
            for row in rows:
                package = self._decode_package(row)
                if not self._agent_supports_capability(capabilities, package["capability"]):
                    continue
                if not self._dependencies_ready(conn, package["dependency_ids"]):
                    continue
                return package
        return None

    def _get_open_assignment_id(
        self, conn: Any, package_id: int, agent_id: int
    ) -> int:
        row = conn.execute(
            """
            SELECT id
            FROM assignments
            WHERE package_id = ?
              AND agent_id = ?
              AND status IN (?, ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                package_id,
                agent_id,
                AssignmentStatus.ASSIGNED.value,
                AssignmentStatus.ACTIVE.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(f"No open assignment for package {package_id} and agent {agent_id}")
        return int(row["id"])

    def assign_package(self, package_id: int, agent_id: int, lease_seconds: int = 900) -> int:
        now = _utcnow()
        lease_expires_at = _as_timestamp(now + timedelta(seconds=lease_seconds))
        with self.connect() as conn:
            package = conn.execute(
                "SELECT goal_id, status FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            agent = conn.execute(
                "SELECT name, current_package_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if package is None:
                raise ValueError(f"Unknown package {package_id}")
            if agent is None:
                raise ValueError(f"Unknown agent {agent_id}")
            if package["status"] != PackageStatus.PENDING.value:
                raise ValueError(f"Package {package_id} is not pending")
            if agent["current_package_id"] is not None:
                raise ValueError(f"Agent {agent_id} is already working on a package")

            cursor = conn.execute(
                """
                INSERT INTO assignments (package_id, agent_id, status, assigned_at, lease_expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    package_id,
                    agent_id,
                    AssignmentStatus.ASSIGNED.value,
                    _as_timestamp(now),
                    lease_expires_at,
                ),
            )
            assignment_id = int(cursor.lastrowid)

            conn.execute(
                """
                UPDATE work_packages
                SET status = ?, assignment_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    PackageStatus.ASSIGNED.value,
                    assignment_id,
                    _as_timestamp(now),
                    package_id,
                ),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_package_id = ?, last_heartbeat_at = ?
                WHERE id = ?
                """,
                (
                    AgentStatus.BUSY.value,
                    package_id,
                    _as_timestamp(now),
                    agent_id,
                ),
            )
            self._record_event(
                conn,
                "work_package",
                package_id,
                EventType.PACKAGE_ASSIGNED.value,
                {"agent_id": agent_id, "assignment_id": assignment_id},
            )
            self._refresh_goal_status(conn, int(package["goal_id"]))
            return assignment_id

    def mark_assignment_active(self, agent_id: int) -> None:
        with self.connect() as conn:
            agent = conn.execute(
                "SELECT current_package_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if agent is None or agent["current_package_id"] is None:
                return
            package_id = int(agent["current_package_id"])
            assignment_id = self._get_open_assignment_id(conn, package_id, agent_id)
            package_row = conn.execute(
                "SELECT goal_id FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            conn.execute(
                "UPDATE assignments SET status = ? WHERE id = ?",
                (AssignmentStatus.ACTIVE.value, assignment_id),
            )
            conn.execute(
                """
                UPDATE work_packages
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (PackageStatus.ACTIVE.value, _as_timestamp(), package_id),
            )
            self._record_event(
                conn,
                "work_package",
                package_id,
                EventType.PACKAGE_ACTIVE.value,
                {"agent_id": agent_id, "assignment_id": assignment_id},
            )
            self._refresh_goal_status(conn, int(package_row["goal_id"]))

    def complete_current_package(self, agent_id: int, summary: str) -> None:
        now = _as_timestamp()
        with self.connect() as conn:
            agent = conn.execute(
                "SELECT current_package_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if agent is None or agent["current_package_id"] is None:
                raise ValueError(f"Agent {agent_id} has no current package")
            package_id = int(agent["current_package_id"])
            assignment_id = self._get_open_assignment_id(conn, package_id, agent_id)
            package = conn.execute(
                "SELECT goal_id FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE assignments
                SET status = ?, completed_at = ?, result_summary = ?
                WHERE id = ?
                """,
                (AssignmentStatus.COMPLETED.value, now, summary, assignment_id),
            )
            conn.execute(
                """
                UPDATE work_packages
                SET status = ?, blocker_reason = NULL, assignment_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (PackageStatus.COMPLETED.value, now, package_id),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_package_id = ?, last_heartbeat_at = ?
                WHERE id = ?
                """,
                (AgentStatus.IDLE.value, None, now, agent_id),
            )
            self._record_event(
                conn,
                "work_package",
                package_id,
                EventType.PACKAGE_COMPLETED.value,
                {"agent_id": agent_id, "summary": summary},
            )
            self._refresh_goal_status(conn, int(package["goal_id"]))

    def block_current_package(self, agent_id: int, reason: str) -> None:
        now = _as_timestamp()
        with self.connect() as conn:
            agent = conn.execute(
                "SELECT current_package_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if agent is None or agent["current_package_id"] is None:
                raise ValueError(f"Agent {agent_id} has no current package")
            package_id = int(agent["current_package_id"])
            assignment_id = self._get_open_assignment_id(conn, package_id, agent_id)
            package_row = conn.execute(
                "SELECT goal_id, metadata FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            metadata = _json_loads(package_row["metadata"], {})
            metadata["blocker_version"] = int(metadata.get("blocker_version", 0)) + 1
            conn.execute(
                """
                UPDATE assignments
                SET status = ?, completed_at = ?, result_summary = ?
                WHERE id = ?
                """,
                (AssignmentStatus.BLOCKED.value, now, reason, assignment_id),
            )
            conn.execute(
                """
                UPDATE work_packages
                SET status = ?, blocker_reason = ?, assignment_id = NULL, metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    PackageStatus.BLOCKED.value,
                    reason,
                    _json_dumps(metadata),
                    now,
                    package_id,
                ),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_package_id = ?, last_heartbeat_at = ?
                WHERE id = ?
                """,
                (AgentStatus.IDLE.value, None, now, agent_id),
            )
            self._record_event(
                conn,
                "work_package",
                package_id,
                EventType.PACKAGE_BLOCKED.value,
                {"agent_id": agent_id, "reason": reason, "blocker_version": metadata["blocker_version"]},
            )
            self._refresh_goal_status(conn, int(package_row["goal_id"]))

    def requeue_package(self, package_id: int, reason: str) -> None:
        now = _as_timestamp()
        with self.connect() as conn:
            package = conn.execute(
                "SELECT goal_id FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
            if package is None:
                raise ValueError(f"Unknown package {package_id}")
            conn.execute(
                """
                UPDATE work_packages
                SET status = ?, blocker_reason = NULL, assignment_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (PackageStatus.PENDING.value, now, package_id),
            )
            self._record_event(
                conn,
                "work_package",
                package_id,
                EventType.PACKAGE_REQUEUED.value,
                {"reason": reason},
            )
            self._refresh_goal_status(conn, int(package["goal_id"]))

    def expire_assignments(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now_value = now or _utcnow()
        expired: List[Dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, wp.goal_id, ag.current_package_id
                FROM assignments a
                JOIN work_packages wp ON wp.id = a.package_id
                JOIN agents ag ON ag.id = a.agent_id
                WHERE a.status IN (?, ?)
                  AND a.lease_expires_at < ?
                ORDER BY a.id ASC
                """,
                (
                    AssignmentStatus.ASSIGNED.value,
                    AssignmentStatus.ACTIVE.value,
                    _as_timestamp(now_value),
                ),
            ).fetchall()
            for row in rows:
                assignment = self._decode_assignment(row)
                conn.execute(
                    """
                    UPDATE assignments
                    SET status = ?, completed_at = ?, result_summary = ?
                    WHERE id = ?
                    """,
                    (
                        AssignmentStatus.EXPIRED.value,
                        _as_timestamp(now_value),
                        "Lease expired",
                        assignment["id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE work_packages
                    SET status = ?, assignment_id = NULL, updated_at = ?
                    WHERE id = ?
                      AND status IN (?, ?)
                    """,
                    (
                        PackageStatus.PENDING.value,
                        _as_timestamp(now_value),
                        assignment["package_id"],
                        PackageStatus.ASSIGNED.value,
                        PackageStatus.ACTIVE.value,
                    ),
                )
                conn.execute(
                    """
                    UPDATE agents
                    SET status = ?, current_package_id = CASE
                        WHEN current_package_id = ? THEN NULL
                        ELSE current_package_id
                    END
                    WHERE id = ?
                    """,
                    (
                        AgentStatus.DEGRADED.value,
                        assignment["package_id"],
                        assignment["agent_id"],
                    ),
                )
                self._record_event(
                    conn,
                    "assignment",
                    assignment["id"],
                    EventType.ASSIGNMENT_EXPIRED.value,
                    {"package_id": assignment["package_id"], "agent_id": assignment["agent_id"]},
                )
                self._refresh_goal_status(conn, int(row["goal_id"]))
                expired.append(assignment)
        return expired

    def find_blocked_packages_without_resolution(self) -> List[Dict[str, Any]]:
        blocked_packages = self.list_packages(statuses=[PackageStatus.BLOCKED.value])
        result: List[Dict[str, Any]] = []
        for blocked in blocked_packages:
            if blocked["kind"] == "unblock":
                continue
            blocker_version = int(blocked["metadata"].get("blocker_version", 0))
            if blocker_version <= 0:
                continue
            children = self.list_child_packages(
                blocked["id"],
                kind="unblock",
            )
            if not any(child["metadata"].get("parent_blocker_version") == blocker_version for child in children):
                result.append(blocked)
        return result

    def find_blocked_packages_ready_for_requeue(self) -> List[Dict[str, Any]]:
        blocked_packages = self.list_packages(statuses=[PackageStatus.BLOCKED.value])
        ready: List[Dict[str, Any]] = []
        for blocked in blocked_packages:
            blocker_version = int(blocked["metadata"].get("blocker_version", 0))
            if blocker_version == 0:
                continue
            children = self.list_child_packages(
                blocked["id"],
                statuses=[PackageStatus.COMPLETED.value],
                kind="unblock",
            )
            if any(
                child["metadata"].get("parent_blocker_version") == blocker_version
                for child in children
            ):
                ready.append(blocked)
        return ready

    def create_resolution_package(
        self,
        blocked_package_id: int,
        capability: str = "orchestrator",
    ) -> int:
        blocked = self.get_package(blocked_package_id)
        if blocked is None:
            raise ValueError(f"Unknown blocked package {blocked_package_id}")
        blocker_version = int(blocked["metadata"].get("blocker_version", 1))
        return self.create_work_package(
            goal_id=blocked["goal_id"],
            title=f"Resolve blocker for: {blocked['title']}",
            description=blocked["blocker_reason"] or "No blocker reason recorded.",
            capability=capability,
            priority=min(blocked["priority"] + 5, 100),
            kind="unblock",
            metadata={
                "parent_blocker_version": blocker_version,
                "blocked_package_id": blocked["id"],
                "original_capability": blocked["capability"],
            },
            parent_package_id=blocked["id"],
        )

    def _refresh_goal_status(self, conn: Any, goal_id: int) -> str:
        rows = conn.execute(
            "SELECT status FROM work_packages WHERE goal_id = ?",
            (goal_id,),
        ).fetchall()
        statuses = [row["status"] for row in rows]
        if not statuses:
            new_status = GoalStatus.NEW.value
        elif all(status == PackageStatus.COMPLETED.value for status in statuses):
            new_status = GoalStatus.COMPLETED.value
        elif any(
            status in (PackageStatus.PENDING.value, PackageStatus.ASSIGNED.value, PackageStatus.ACTIVE.value)
            for status in statuses
        ):
            new_status = GoalStatus.ACTIVE.value
        elif any(status == PackageStatus.BLOCKED.value for status in statuses):
            new_status = GoalStatus.BLOCKED.value
        else:
            new_status = GoalStatus.ACTIVE.value

        current = conn.execute(
            "SELECT status FROM goals WHERE id = ?",
            (goal_id,),
        ).fetchone()
        if current and current["status"] != new_status:
            conn.execute(
                """
                UPDATE goals
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, _as_timestamp(), goal_id),
            )
            self._record_event(
                conn,
                "goal",
                goal_id,
                EventType.GOAL_STATUS_CHANGED.value,
                {"status": new_status},
            )
        elif current:
            conn.execute(
                "UPDATE goals SET updated_at = ? WHERE id = ?",
                (_as_timestamp(), goal_id),
            )
        return new_status

    def refresh_goal_status(self, goal_id: int) -> str:
        with self.connect() as conn:
            return self._refresh_goal_status(conn, goal_id)
