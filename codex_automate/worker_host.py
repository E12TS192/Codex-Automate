from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from codex_automate.database import is_postgres_target, redact_database_target, resolve_database_target
from codex_automate.state import StateStore


def _env_flag(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _split_agent_names(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class WorkerHostConfig:
    database_target: str
    workspace_root: Path
    poll_seconds: float = 5.0
    max_cycles: Optional[int] = None
    goal_id: Optional[int] = None
    agent_names: Tuple[str, ...] = ()
    stop_when_idle: bool = False
    require_persistent_db: bool = True


def resolve_worker_host_config(
    *,
    env: Optional[Mapping[str, str]] = None,
    database_target: Optional[str] = None,
    workspace_root: Optional[str] = None,
) -> WorkerHostConfig:
    source = env or os.environ
    root = Path(workspace_root or source.get("CODEX_AUTOMATE_WORKSPACE") or Path.cwd()).resolve()
    resolved_database_target = (
        database_target
        or source.get("CODEX_AUTOMATE_DATABASE_URL")
        or source.get("DATABASE_URL")
        or source.get("POSTGRES_URL")
    )
    max_cycles_value = source.get("CODEX_AUTOMATE_MAX_CYCLES")
    goal_id_value = source.get("CODEX_AUTOMATE_GOAL_ID")
    return WorkerHostConfig(
        database_target=resolve_database_target(resolved_database_target),
        workspace_root=root,
        poll_seconds=float(source.get("CODEX_AUTOMATE_POLL_SECONDS", "5")),
        max_cycles=int(max_cycles_value) if max_cycles_value else None,
        goal_id=int(goal_id_value) if goal_id_value else None,
        agent_names=_split_agent_names(source.get("CODEX_AUTOMATE_AGENT_NAMES")),
        stop_when_idle=_env_flag(source.get("CODEX_AUTOMATE_STOP_WHEN_IDLE"), default=False),
        require_persistent_db=_env_flag(source.get("CODEX_AUTOMATE_REQUIRE_PERSISTENT_DB"), default=True),
    )


def inspect_worker_host(config: WorkerHostConfig) -> Dict[str, Any]:
    if not config.workspace_root.exists():
        raise ValueError(f"Workspace does not exist: {config.workspace_root}")
    if not config.workspace_root.is_dir():
        raise ValueError(f"Workspace is not a directory: {config.workspace_root}")
    if config.require_persistent_db and not is_postgres_target(config.database_target):
        raise ValueError(
            "Worker host requires a persistent Postgres database. "
            "Set CODEX_AUTOMATE_DATABASE_URL or disable the guard with "
            "CODEX_AUTOMATE_REQUIRE_PERSISTENT_DB=0 for local-only usage."
        )

    store = StateStore(config.database_target)
    store.initialize()
    agents = store.list_agents()
    codex_exec_agents = sorted(
        agent["name"]
        for agent in agents
        if dict(agent.get("metadata", {})).get("runner", {}).get("type", "codex_exec") == "codex_exec"
    )
    shell_agents = sorted(
        agent["name"]
        for agent in agents
        if dict(agent.get("metadata", {})).get("runner", {}).get("type") == "shell"
    )
    codex_available = shutil.which("codex") is not None
    if codex_exec_agents and not codex_available:
        raise ValueError(
            "Registered codex_exec agents require the 'codex' CLI on PATH. "
            f"Affected agents: {', '.join(codex_exec_agents)}"
        )

    return {
        "ok": True,
        "workspace": str(config.workspace_root),
        "database": redact_database_target(config.database_target),
        "backend": store.backend,
        "require_persistent_db": config.require_persistent_db,
        "poll_seconds": config.poll_seconds,
        "max_cycles": config.max_cycles,
        "goal_id": config.goal_id,
        "agent_names": list(config.agent_names),
        "agent_count": len(agents),
        "codex_exec_agents": codex_exec_agents,
        "shell_agents": shell_agents,
        "codex_available": codex_available,
    }


def build_serve_workers_command(
    config: WorkerHostConfig,
    *,
    python_bin: str = "python3",
    module: str = "codex_automate",
) -> Sequence[str]:
    command = [
        python_bin,
        "-m",
        module,
        "serve-workers",
        "--workspace",
        str(config.workspace_root),
        "--poll-seconds",
        str(config.poll_seconds),
    ]
    if config.max_cycles is not None:
        command.extend(["--max-cycles", str(config.max_cycles)])
    if config.goal_id is not None:
        command.extend(["--goal-id", str(config.goal_id)])
    if config.stop_when_idle:
        command.append("--stop-when-idle")
    for agent_name in config.agent_names:
        command.extend(["--agent", agent_name])
    return command
