from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalStatus(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class PackageStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AssignmentStatus(str, Enum):
    ASSIGNED = "assigned"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    EXPIRED = "expired"


class EventType(str, Enum):
    GOAL_CREATED = "goal.created"
    GOAL_STATUS_CHANGED = "goal.status_changed"
    PACKAGE_CREATED = "package.created"
    PACKAGE_ASSIGNED = "package.assigned"
    PACKAGE_ACTIVE = "package.active"
    PACKAGE_BLOCKED = "package.blocked"
    PACKAGE_COMPLETED = "package.completed"
    PACKAGE_REQUEUED = "package.requeued"
    AGENT_REGISTERED = "agent.registered"
    AGENT_HEARTBEAT = "agent.heartbeat"
    ASSIGNMENT_EXPIRED = "assignment.expired"
    ORCHESTRATOR_ACTION = "orchestrator.action"


@dataclass
class WorkPackageInput:
    title: str
    description: str
    capability: str
    priority: int = 50
    kind: str = "delivery"
    key: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GoalInput:
    title: str
    objective: str
    acceptance_criteria: List[str] = field(default_factory=list)
    packages: List[WorkPackageInput] = field(default_factory=list)

