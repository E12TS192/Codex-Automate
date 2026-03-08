from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional

from codex_automate.models import AgentStatus, GoalStatus
from codex_automate.orchestrator import Orchestrator
from codex_automate.state import StateStore


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class WorkerRuntime:
    def __init__(
        self,
        store: StateStore,
        workspace_root: str,
        orchestrator: Optional[Orchestrator] = None,
        schema_path: Optional[str] = None,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.orchestrator = orchestrator or Orchestrator(store)
        self.schema_path = Path(schema_path).resolve() if schema_path else (
            Path(__file__).resolve().parent / "schemas" / "worker_result.schema.json"
        )

    def _resolve_runner_config(self, agent: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(agent.get("metadata", {}))
        runner = dict(metadata.get("runner", {}))
        runner.setdefault("type", "codex_exec")
        runner.setdefault("sandbox", "workspace-write")
        runner.setdefault("instructions", [])
        runner.setdefault("add_dirs", [])
        runner.setdefault("ephemeral", True)
        runner.setdefault("skip_git_repo_check", True)
        return runner

    def _resolve_path(self, maybe_path: str) -> Path:
        path = Path(maybe_path)
        if path.is_absolute():
            return path
        return (self.workspace_root / path).resolve()

    def _build_run_dir(self, package: Dict[str, Any], agent: Dict[str, Any]) -> Path:
        safe_agent_name = agent["name"].replace("/", "-").replace(" ", "-")
        run_dir = (
            self.workspace_root
            / "state"
            / "runs"
            / f"goal-{package['goal_id']}"
            / f"package-{package['id']}-{safe_agent_name}-{_utc_stamp()}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _build_context(self, agent: Dict[str, Any], goal: Dict[str, Any], package: Dict[str, Any], run_dir: Path) -> Dict[str, Any]:
        dependencies = [
            self.store.get_package(dependency_id)
            for dependency_id in package["dependency_ids"]
            if self.store.get_package(dependency_id) is not None
        ]
        sibling_packages = [
            {
                "id": item["id"],
                "title": item["title"],
                "status": item["status"],
                "capability": item["capability"],
            }
            for item in self.store.list_packages(goal_id=goal["id"])
            if item["id"] != package["id"]
        ]
        return {
            "goal": goal,
            "package": package,
            "dependencies": dependencies,
            "sibling_packages": sibling_packages,
            "agent": {
                "name": agent["name"],
                "capabilities": agent["capabilities"],
                "runner": self._resolve_runner_config(agent),
            },
            "artifact_dir": str(run_dir),
            "workspace_root": str(self.workspace_root),
        }

    def _build_prompt(self, context: Dict[str, Any]) -> str:
        runner = context["agent"]["runner"]
        extra_instructions = runner.get("instructions") or ["Stay tightly within the assigned package."]
        instruction_block = "\n".join(f"- {item}" for item in extra_instructions)
        return dedent(
            f"""\
            You are worker agent '{context['agent']['name']}'.
            Work only on the assigned package. Do not re-plan the whole goal.

            Agent instructions:
            {instruction_block}

            Goal:
            {json.dumps(context['goal'], indent=2, ensure_ascii=True)}

            Assigned package:
            {json.dumps(context['package'], indent=2, ensure_ascii=True)}

            Dependency packages:
            {json.dumps(context['dependencies'], indent=2, ensure_ascii=True)}

            Other packages in the same goal:
            {json.dumps(context['sibling_packages'], indent=2, ensure_ascii=True)}

            Workspace root: {context['workspace_root']}
            Artifact directory: {context['artifact_dir']}

            Rules:
            - Finish only this package.
            - If a real blocker prevents completion, stop and report status='blocked'.
            - If completed, summarize the concrete outcome.
            - Always include blocker_reason. Use an empty string when the package is completed.
            - Save any supporting artifacts under the artifact directory when useful.
            - Your final response must satisfy the provided JSON schema.
            """
        )

    def _write_run_inputs(self, run_dir: Path, context: Dict[str, Any]) -> Dict[str, Path]:
        prompt_path = run_dir / "prompt.txt"
        context_path = run_dir / "context.json"
        prompt_path.write_text(self._build_prompt(context), encoding="utf-8")
        context_path.write_text(json.dumps(context, indent=2, ensure_ascii=True), encoding="utf-8")
        return {
            "prompt": prompt_path,
            "context": context_path,
            "result": run_dir / "result.json",
            "stdout": run_dir / "stdout.log",
            "stderr": run_dir / "stderr.log",
            "command": run_dir / "command.json",
        }

    def _format_shell_command(self, command: str, paths: Dict[str, Path], run_dir: Path) -> str:
        values = {
            "prompt_file": str(paths["prompt"]),
            "context_file": str(paths["context"]),
            "result_file": str(paths["result"]),
            "stdout_file": str(paths["stdout"]),
            "stderr_file": str(paths["stderr"]),
            "run_dir": str(run_dir),
            "workspace": str(self.workspace_root),
            "prompt_file_q": shlex.quote(str(paths["prompt"])),
            "context_file_q": shlex.quote(str(paths["context"])),
            "result_file_q": shlex.quote(str(paths["result"])),
            "stdout_file_q": shlex.quote(str(paths["stdout"])),
            "stderr_file_q": shlex.quote(str(paths["stderr"])),
            "run_dir_q": shlex.quote(str(run_dir)),
            "workspace_q": shlex.quote(str(self.workspace_root)),
        }
        if not any(f"{{{key}}}" in command for key in values):
            return command
        return command.format(**values)

    def _shell_env(self, paths: Dict[str, Path], run_dir: Path) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_AUTOMATE_PROMPT_FILE": str(paths["prompt"]),
                "CODEX_AUTOMATE_CONTEXT_FILE": str(paths["context"]),
                "CODEX_AUTOMATE_RESULT_FILE": str(paths["result"]),
                "CODEX_AUTOMATE_STDOUT_FILE": str(paths["stdout"]),
                "CODEX_AUTOMATE_STDERR_FILE": str(paths["stderr"]),
                "CODEX_AUTOMATE_RUN_DIR": str(run_dir),
                "CODEX_AUTOMATE_WORKSPACE": str(self.workspace_root),
            }
        )
        return env

    def _resolve_cwd(self, runner: Dict[str, Any]) -> Path:
        if runner.get("cwd"):
            return self._resolve_path(runner["cwd"])
        return self.workspace_root

    def _run_shell_runner(self, runner: Dict[str, Any], paths: Dict[str, Path], run_dir: Path) -> subprocess.CompletedProcess[str]:
        command = runner.get("command")
        if not command:
            raise ValueError("Shell runner requires a command.")
        formatted_command = self._format_shell_command(command, paths, run_dir)
        paths["command"].write_text(
            json.dumps({"runner_type": "shell", "command": formatted_command}, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        completed = subprocess.run(
            formatted_command,
            cwd=str(self._resolve_cwd(runner)),
            env=self._shell_env(paths, run_dir),
            shell=True,
            executable="/bin/zsh",
            text=True,
            capture_output=True,
        )
        paths["stdout"].write_text(completed.stdout, encoding="utf-8")
        paths["stderr"].write_text(completed.stderr, encoding="utf-8")
        return completed

    def _run_codex_exec(self, runner: Dict[str, Any], paths: Dict[str, Path]) -> subprocess.CompletedProcess[str]:
        command: List[str] = [
            "codex",
            "exec",
            "--json",
            "--color",
            "never",
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(paths["result"]),
        ]
        if runner.get("sandbox"):
            command.extend(["-s", runner["sandbox"]])
        if runner.get("model"):
            command.extend(["-m", runner["model"]])
        if runner.get("ephemeral", True):
            command.append("--ephemeral")
        if runner.get("skip_git_repo_check", True):
            command.append("--skip-git-repo-check")
        for add_dir in runner.get("add_dirs", []):
            command.extend(["--add-dir", str(self._resolve_path(add_dir))])
        command.extend(["-C", str(self._resolve_cwd(runner)), "-"])
        paths["command"].write_text(
            json.dumps({"runner_type": "codex_exec", "command": command}, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        completed = subprocess.run(
            command,
            cwd=str(self._resolve_cwd(runner)),
            env=self._shell_env(paths, paths["command"].parent),
            input=paths["prompt"].read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
        )
        paths["stdout"].write_text(completed.stdout, encoding="utf-8")
        paths["stderr"].write_text(completed.stderr, encoding="utf-8")
        return completed

    def _load_result_payload(self, result_path: Path) -> Dict[str, Any]:
        if not result_path.exists():
            raise ValueError(f"Worker result file missing: {result_path}")
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if payload.get("status") not in ("completed", "blocked"):
            raise ValueError("Worker result status must be 'completed' or 'blocked'.")
        if not payload.get("summary"):
            raise ValueError("Worker result must contain a summary.")
        payload.setdefault("blocker_reason", "")
        if payload.get("status") == "blocked" and not payload.get("blocker_reason"):
            raise ValueError("Blocked worker results must contain blocker_reason.")
        payload.setdefault("artifacts", [])
        payload.setdefault("notes", [])
        return payload

    def _append_run_metadata(
        self,
        package_id: int,
        run_dir: Path,
        runner_type: str,
        payload: Dict[str, Any],
        return_code: int,
    ) -> None:
        package = self.store.get_package(package_id)
        if package is None:
            return
        metadata = dict(package["metadata"])
        runs = list(metadata.get("runs", []))
        run_record = {
            "run_dir": str(run_dir),
            "runner_type": runner_type,
            "status": payload.get("status"),
            "summary": payload.get("summary"),
            "artifacts": payload.get("artifacts", []),
            "notes": payload.get("notes", []),
            "return_code": return_code,
        }
        runs.append(run_record)
        metadata["runs"] = runs[-20:]
        metadata["latest_run"] = run_record
        self.store.update_package_metadata(package_id, metadata)

    def _runner_error_payload(self, return_code: int, run_dir: Path, stderr_path: Path) -> Dict[str, Any]:
        error_excerpt = stderr_path.read_text(encoding="utf-8").strip()
        if error_excerpt:
            error_excerpt = error_excerpt.splitlines()[-1]
        else:
            error_excerpt = "No stderr output captured."
        return {
            "status": "blocked",
            "summary": "Worker execution failed",
            "blocker_reason": f"Runner failed with exit code {return_code}. See {run_dir}. Last stderr line: {error_excerpt}",
            "artifacts": [{"path": str(stderr_path), "description": "Runner stderr"}],
            "notes": [],
        }

    def run_agent_once(self, agent_name: str) -> Dict[str, Any]:
        agent = self.store.get_agent_by_name(agent_name)
        if agent is None:
            raise ValueError(f"Unknown agent '{agent_name}'")

        package = self.store.get_current_package_for_agent(agent["id"])
        current_status = AgentStatus.BUSY.value if package else AgentStatus.IDLE.value
        self.store.heartbeat(agent["id"], status=current_status)
        if package is None:
            return {
                "agent_name": agent_name,
                "package_id": None,
                "outcome": "idle",
                "summary": "No assigned package",
                "run_dir": None,
            }

        goal = self.store.get_goal(package["goal_id"])
        if goal is None:
            raise ValueError(f"Goal {package['goal_id']} for package {package['id']} is missing")

        run_dir = self._build_run_dir(package, agent)
        context = self._build_context(agent, goal, package, run_dir)
        paths = self._write_run_inputs(run_dir, context)
        runner = self._resolve_runner_config(agent)

        self.store.mark_assignment_active(agent["id"])
        if runner["type"] == "shell":
            completed = self._run_shell_runner(runner, paths, run_dir)
        elif runner["type"] == "codex_exec":
            completed = self._run_codex_exec(runner, paths)
        else:
            raise ValueError(f"Unsupported runner type: {runner['type']}")

        if completed.returncode == 0:
            try:
                payload = self._load_result_payload(paths["result"])
            except Exception as exc:
                payload = {
                    "status": "blocked",
                    "summary": "Worker produced an invalid result",
                    "blocker_reason": f"{exc}. See {run_dir}",
                    "artifacts": [{"path": str(paths["result"]), "description": "Invalid worker result"}],
                    "notes": [],
                }
        else:
            payload = self._runner_error_payload(completed.returncode, run_dir, paths["stderr"])

        self._append_run_metadata(
            package_id=package["id"],
            run_dir=run_dir,
            runner_type=runner["type"],
            payload=payload,
            return_code=completed.returncode,
        )

        if payload["status"] == "completed":
            self.store.complete_current_package(agent["id"], payload["summary"])
            outcome = "completed"
        else:
            blocker_reason = payload.get("blocker_reason", "Worker reported blocked.")
            self.store.block_current_package(agent["id"], blocker_reason)
            outcome = "blocked"

        return {
            "agent_name": agent_name,
            "package_id": package["id"],
            "package_title": package["title"],
            "outcome": outcome,
            "summary": payload["summary"],
            "run_dir": str(run_dir),
        }

    def run_autopilot(self, goal_id: Optional[int] = None, max_iterations: int = 10) -> Dict[str, Any]:
        dashboard = self.orchestrator.dashboard(goal_id=goal_id)
        if dashboard["goal"] is None:
            raise ValueError("No goal available for autopilot.")
        target_goal_id = dashboard["goal"]["id"]

        timeline: List[Dict[str, Any]] = []
        for iteration in range(1, max_iterations + 1):
            tick_result = self.orchestrator.tick()
            worker_results: List[Dict[str, Any]] = []
            for agent in self.store.list_agents():
                if agent["current_package_id"] is None:
                    continue
                worker_results.append(self.run_agent_once(agent["name"]))
            dashboard = self.orchestrator.dashboard(goal_id=target_goal_id)
            timeline.append(
                {
                    "iteration": iteration,
                    "tick": tick_result,
                    "worker_results": worker_results,
                    "goal_status": dashboard["goal"]["status"] if dashboard["goal"] else None,
                }
            )
            if dashboard["goal"] and dashboard["goal"]["status"] == GoalStatus.COMPLETED.value:
                break
            if (
                not tick_result["assignments"]
                and not tick_result["resolution_packages"]
                and not tick_result["requeued_packages"]
                and not worker_results
            ):
                break

        return {
            "goal_id": target_goal_id,
            "timeline": timeline,
            "dashboard": dashboard,
        }
