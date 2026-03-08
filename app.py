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
    return get_orchestrator().dashboard(goal_id=goal_id)


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


@app.post("/api/tick", dependencies=[Depends(require_operator_access)])
def tick() -> Dict[str, Any]:
    return get_orchestrator().tick()
