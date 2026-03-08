from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
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
INDEX_HTML = (Path(__file__).resolve().parent / "public" / "index.html").read_text(encoding="utf-8")


@lru_cache
def get_store() -> StateStore:
    target = resolve_database_target()
    store = StateStore(target)
    store.initialize()
    return store


def get_orchestrator() -> Orchestrator:
    return Orchestrator(get_store())


@app.get("/", response_class=HTMLResponse)
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


@app.get("/api/dashboard")
def dashboard(goal_id: Optional[int] = None) -> Dict[str, Any]:
    store = get_store()
    payload = get_orchestrator().dashboard(goal_id=goal_id)
    payload["meta"] = {
        "backend": store.backend,
        "database": redact_database_target(store.database_target),
    }
    return payload


@app.post("/api/goals")
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


@app.post("/api/agents")
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


@app.post("/api/tick")
def tick() -> Dict[str, Any]:
    return get_orchestrator().tick()
