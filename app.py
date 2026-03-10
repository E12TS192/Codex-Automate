from __future__ import annotations

import os
import secrets
from functools import lru_cache
from importlib.resources import files
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from codex_automate.database import redact_database_target, resolve_database_target
from codex_automate.orchestrator import Orchestrator
from codex_automate.state import StateStore


class AgentPayload(BaseModel):
    name: str
    capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GoalPayload(BaseModel):
    title: str
    objective: Optional[str] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    packages: List[Dict[str, Any]] = Field(default_factory=list)


class ManualPackagePayload(BaseModel):
    title: str
    description: str
    capability: str
    priority: int = 50
    kind: str = "delivery"
    acceptance_criteria: List[str] = Field(default_factory=list)
    dependency_ids: List[int] = Field(default_factory=list)
    preferred_agent_name: Optional[str] = None


class OperatorNotePayload(BaseModel):
    goal_id: Optional[int] = None
    package_id: Optional[int] = None
    agent_id: Optional[int] = None
    kind: str
    title: str
    body: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TokenBudgetPayload(BaseModel):
    scope_type: str
    scope_id: Optional[int] = None
    input_limit: Optional[int] = None
    output_limit: Optional[int] = None
    total_limit: Optional[int] = None
    enabled: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RequeuePayload(BaseModel):
    reason: str = "Manual operator retry"


app = FastAPI(title="Codex Automate Control Plane", version="0.2.0")
INDEX_HTML = files("codex_automate.assets").joinpath("dashboard.html").read_text(encoding="utf-8")
BASIC_AUTH = HTTPBasic(auto_error=False)


def _auth_required() -> bool:
    if os.getenv("CODEX_AUTOMATE_REQUIRE_AUTH") is not None:
        return os.getenv("CODEX_AUTOMATE_REQUIRE_AUTH", "").strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(os.getenv("VERCEL"))


def _auth_configured() -> bool:
    return bool(os.getenv("CODEX_AUTOMATE_AUTH_USERNAME") and os.getenv("CODEX_AUTOMATE_AUTH_PASSWORD"))


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Basic"},
    )


def require_operator_access(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(BASIC_AUTH),
) -> None:
    if request.url.path == "/api/health":
        return
    if not _auth_required():
        return
    if not _auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Dashboard auth is required, but CODEX_AUTOMATE_AUTH_USERNAME / "
                "CODEX_AUTOMATE_AUTH_PASSWORD are not configured."
            ),
        )
    if credentials is None:
        raise _unauthorized("Authentication required.")

    expected_username = os.getenv("CODEX_AUTOMATE_AUTH_USERNAME", "")
    expected_password = os.getenv("CODEX_AUTOMATE_AUTH_PASSWORD", "")
    username_ok = secrets.compare_digest(credentials.username, expected_username)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (username_ok and password_ok):
        raise _unauthorized("Invalid credentials.")


@lru_cache
def get_store() -> StateStore:
    target = resolve_database_target()
    store = StateStore(target)
    store.initialize()
    return store


def get_orchestrator() -> Orchestrator:
    return Orchestrator(get_store())


def _empty_usage_summary() -> Dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "run_count": 0,
    }


def _dashboard_payload(goal_id: Optional[int] = None) -> Dict[str, Any]:
    store = get_store()
    orchestrator = get_orchestrator()
    payload = orchestrator.dashboard(goal_id=goal_id)
    selected_goal = payload["goal"]
    selected_goal_id = selected_goal["id"] if selected_goal else None

    global_usage = store.summarize_token_usage()
    goal_usage = store.summarize_token_usage(goal_id=selected_goal_id) if selected_goal_id is not None else _empty_usage_summary()
    budgets = store.list_token_budgets()
    budget_status: List[Dict[str, Any]] = []
    for budget in budgets:
        scope_type = budget["scope_type"]
        scope_id = budget["scope_id"]
        if scope_type == "global":
            usage = global_usage
        elif scope_type == "goal":
            usage = store.summarize_token_usage(goal_id=scope_id) if scope_id is not None else _empty_usage_summary()
        elif scope_type == "agent":
            usage = store.summarize_token_usage(agent_id=scope_id) if scope_id is not None else _empty_usage_summary()
        else:
            usage = _empty_usage_summary()
        budget_status.append(
            {
                "budget": budget,
                "usage": usage,
                "exceeded": {
                    "input": budget["input_limit"] is not None and usage["input_tokens"] >= int(budget["input_limit"]),
                    "output": budget["output_limit"] is not None and usage["output_tokens"] >= int(budget["output_limit"]),
                    "total": budget["total_limit"] is not None and usage["total_tokens"] >= int(budget["total_limit"]),
                },
            }
        )

    payload["operator_notes"] = (
        store.list_operator_notes(goal_id=selected_goal_id, limit=80)
        if selected_goal_id is not None
        else store.list_operator_notes(limit=80)
    )
    payload["token_usage"] = {
        "global": global_usage,
        "goal": goal_usage,
        "recent_runs": (
            store.list_token_usage(goal_id=selected_goal_id, limit=30)
            if selected_goal_id is not None
            else store.list_token_usage(limit=30)
        ),
        "budgets": budgets,
        "budget_status": budget_status,
    }
    return payload


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_operator_access)])
def index() -> str:
    return INDEX_HTML


@app.get("/api/health")
def health() -> Dict[str, Any]:
    store = get_store()
    return {
        "ok": True,
        "backend": store.backend,
        "database": redact_database_target(store.database_target),
    }


@app.get("/api/dashboard", dependencies=[Depends(require_operator_access)])
def dashboard(goal_id: Optional[int] = None) -> Dict[str, Any]:
    return _dashboard_payload(goal_id=goal_id)


@app.post("/api/goals", dependencies=[Depends(require_operator_access)])
def submit_goal(payload: GoalPayload) -> Dict[str, Any]:
    goal_data = payload.model_dump()
    if goal_data["objective"] is None:
        goal_data["objective"] = goal_data["title"]
    try:
        goal_id = get_orchestrator().submit_goal_from_dict(goal_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "goal_id": goal_id,
        "goal": get_store().get_goal(goal_id),
    }


@app.post("/api/goals/{goal_id}/packages", dependencies=[Depends(require_operator_access)])
def create_manual_package(goal_id: int, payload: ManualPackagePayload) -> Dict[str, Any]:
    store = get_store()
    goal = store.get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found.")
    metadata = {}
    if payload.preferred_agent_name:
        metadata["preferred_agent_name"] = payload.preferred_agent_name
    package_id = store.create_work_package(
        goal_id=goal_id,
        title=payload.title,
        description=payload.description,
        capability=payload.capability,
        priority=payload.priority,
        kind=payload.kind,
        acceptance_criteria=payload.acceptance_criteria,
        dependency_ids=payload.dependency_ids,
        metadata=metadata,
    )
    return {
        "package_id": package_id,
        "package": store.get_package(package_id),
    }


@app.post("/api/agents", dependencies=[Depends(require_operator_access)])
def register_agent(payload: AgentPayload) -> Dict[str, Any]:
    try:
        agent_id = get_store().register_agent(
            name=payload.name,
            capabilities=payload.capabilities,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "agent": get_store().get_agent(agent_id),
    }


@app.post("/api/notes", dependencies=[Depends(require_operator_access)])
def create_operator_note(payload: OperatorNotePayload) -> Dict[str, Any]:
    store = get_store()
    note_id = store.create_operator_note(
        goal_id=payload.goal_id,
        package_id=payload.package_id,
        agent_id=payload.agent_id,
        kind=payload.kind,
        title=payload.title,
        body=payload.body,
        metadata=payload.metadata,
    )
    note = next((item for item in store.list_operator_notes(limit=1) if item["id"] == note_id), None)
    return {
        "note_id": note_id,
        "note": note,
    }


@app.post("/api/notes/{note_id}/resolve", dependencies=[Depends(require_operator_access)])
def resolve_operator_note(note_id: int) -> Dict[str, Any]:
    store = get_store()
    try:
        store.resolve_operator_note(note_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@app.put("/api/token-budgets", dependencies=[Depends(require_operator_access)])
def upsert_token_budget(payload: TokenBudgetPayload) -> Dict[str, Any]:
    if payload.scope_type not in {"global", "goal", "agent"}:
        raise HTTPException(status_code=400, detail="scope_type must be one of: global, goal, agent")
    if payload.scope_type != "global" and payload.scope_id is None:
        raise HTTPException(status_code=400, detail="scope_id is required for goal or agent budgets")
    budget_id = get_store().upsert_token_budget(
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        input_limit=payload.input_limit,
        output_limit=payload.output_limit,
        total_limit=payload.total_limit,
        enabled=payload.enabled,
        metadata=payload.metadata,
    )
    budget = next((item for item in get_store().list_token_budgets() if item["id"] == budget_id), None)
    return {
        "budget_id": budget_id,
        "budget": budget,
    }


@app.post("/api/packages/{package_id}/requeue", dependencies=[Depends(require_operator_access)])
def requeue_package(package_id: int, payload: RequeuePayload) -> Dict[str, Any]:
    store = get_store()
    package = store.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    store.requeue_package(package_id, payload.reason)
    return {
        "ok": True,
        "package": store.get_package(package_id),
    }


@app.post("/api/tick", dependencies=[Depends(require_operator_access)])
def tick() -> Dict[str, Any]:
    return get_orchestrator().tick()
