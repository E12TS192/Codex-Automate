from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional, Sequence

from codex_automate.models import AgentStatus, GoalStatus, WorkPackageInput
from codex_automate.orchestrator import Orchestrator
from codex_automate.state import StateStore


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class RunnerTimeoutError(RuntimeError):
    def __init__(
        self,
        timeout_seconds: float,
        elapsed_seconds: float,
        return_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(f"Runner timed out after {elapsed_seconds:.1f}s")
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


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

    def _package_snapshot(self, package: Optional[Dict[str, Any]], *, include_siblings: bool = False) -> Optional[Dict[str, Any]]:
        if package is None:
            return None
        metadata = dict(package.get("metadata", {}))
        latest_run = dict(metadata.get("latest_run") or {})
        latest_stage_output = dict(latest_run.get("stage_output") or metadata.get("stage_output") or {})
        latest_notes = [str(item) for item in latest_run.get("notes", [])[:8]]
        snapshot = {
            "id": package["id"],
            "goal_id": package["goal_id"],
            "parent_package_id": package.get("parent_package_id"),
            "title": package["title"],
            "description": package["description"],
            "capability": package["capability"],
            "priority": package["priority"],
            "kind": package["kind"],
            "status": package["status"],
            "acceptance_criteria": list(package.get("acceptance_criteria", [])),
            "dependency_ids": list(package.get("dependency_ids", [])),
            "blocker_reason": package.get("blocker_reason"),
            "metadata": {
                key: metadata[key]
                for key in (
                    "stage",
                    "allow_new_packages",
                    "generated_by_package_id",
                    "blocked_package_id",
                    "parent_blocker_version",
                    "original_capability",
                )
                if key in metadata
            },
            "latest_run": {
                "status": latest_run.get("status"),
                "summary": latest_run.get("summary"),
                "notes": latest_notes,
                "stage_output": latest_stage_output,
            }
            if latest_run or latest_stage_output
            else {},
        }
        if include_siblings:
            return {
                "id": snapshot["id"],
                "title": snapshot["title"],
                "status": snapshot["status"],
                "capability": snapshot["capability"],
            }
        return snapshot

    def _resolve_runner_config(self, agent: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(agent.get("metadata", {}))
        runner = dict(metadata.get("runner", {}))
        runner.setdefault("type", "codex_exec")
        runner.setdefault("sandbox", "workspace-write")
        runner.setdefault("instructions", [])
        runner.setdefault("add_dirs", [])
        runner.setdefault("ephemeral", True)
        runner.setdefault("skip_git_repo_check", True)
        timeout_seconds = runner.get("timeout_seconds")
        if timeout_seconds in (None, ""):
            runner["timeout_seconds"] = float(self.orchestrator.lease_seconds)
        else:
            runner["timeout_seconds"] = float(timeout_seconds)
        heartbeat_interval = runner.get("heartbeat_interval_seconds")
        if heartbeat_interval in (None, ""):
            runner["heartbeat_interval_seconds"] = max(1.0, min(30.0, float(self.orchestrator.lease_seconds) / 3.0))
        else:
            runner["heartbeat_interval_seconds"] = float(heartbeat_interval)
        return runner

    def _select_agents(self, agent_names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        agents = self.store.list_agents()
        if not agent_names:
            return agents
        selected = set(agent_names)
        return [agent for agent in agents if agent["name"] in selected]

    def heartbeat_agents(self, agent_names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        heartbeats: List[Dict[str, Any]] = []
        for agent in self._select_agents(agent_names):
            status = AgentStatus.BUSY.value if agent["current_package_id"] else AgentStatus.IDLE.value
            self.store.heartbeat(agent["id"], status=status, lease_seconds=self.orchestrator.lease_seconds)
            heartbeats.append(
                {
                    "agent_id": agent["id"],
                    "agent_name": agent["name"],
                    "status": status,
                }
            )
        return heartbeats

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
            self._package_snapshot(self.store.get_package(dependency_id))
            for dependency_id in package["dependency_ids"]
            if self.store.get_package(dependency_id) is not None
        ]
        blocked_package = None
        blocked_package_id = dict(package.get("metadata", {})).get("blocked_package_id")
        if blocked_package_id is not None:
            blocked_package = self._package_snapshot(self.store.get_package(int(blocked_package_id)))
        sibling_packages = [
            self._package_snapshot(item, include_siblings=True)
            for item in self.store.list_packages(goal_id=goal["id"])
            if item["id"] != package["id"]
        ]
        return {
            "goal": goal,
            "package": self._package_snapshot(package),
            "dependencies": dependencies,
            "blocked_package": blocked_package,
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
        package_metadata = context["package"].get("metadata", {})
        allow_new_packages = bool(package_metadata.get("allow_new_packages"))
        stage_guidance = self._stage_guidance(context)
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

            Blocked package context:
            {json.dumps(context['blocked_package'], indent=2, ensure_ascii=True)}

            Other packages in the same goal:
            {json.dumps(context['sibling_packages'], indent=2, ensure_ascii=True)}

            Workspace root: {context['workspace_root']}
            Artifact directory: {context['artifact_dir']}

            Rules:
            - Finish only this package.
            - If a real blocker prevents completion, stop and report status='blocked'.
            - If completed, summarize the concrete outcome in summary using one concise executive sentence.
            - Always include blocker_reason. Use an empty string when the package is completed.
            - Put supporting detail into notes as short, decision-ready bullet sentences.
            - Use new_packages only when this package should spawn concrete follow-on work.
            - This package may create follow-on packages: {allow_new_packages}.
            - When allow_new_packages is false, new_packages must stay empty.
            - When allow_new_packages is true and more implementation work is needed, emit concrete new_packages instead of vague notes.
            - Always emit all top-level schema fields, including new_packages and stage_output.
            - If you emit new_packages, every package entry must include all schema keys. Use empty arrays/objects and a non-empty kind string when nothing special is needed.
            - In stage_output, keep irrelevant fields empty or null, but still include them.
            - Prefer the existing package context over fresh exploration. Use only the minimum research needed to answer this package.
            - Save any supporting artifacts under the artifact directory when useful.
            - Your final response must satisfy the provided JSON schema.

            Stage-specific guidance:
            {stage_guidance}
            """
        )

    def _stage_guidance(self, context: Dict[str, Any]) -> str:
        metadata = dict(context["package"].get("metadata", {}))
        stage = metadata.get("stage")
        if context["package"].get("kind") == "unblock":
            return dedent(
                """\
                - This is a blocker-resolution package. Diagnose the blocker using the blocked package context and its existing run artifacts first.
                - Do not do broad new discovery or web research unless the blocker explicitly cannot be resolved from the existing context.
                - For planning or orchestrator blockers, prefer zero new web research and operate only from the existing logs, prompt and context.
                - The goal is an operational next step: either a narrow retry plan, a concrete fix condition, or a concise no-go reason.
                - Keep the summary short and operational.
                - Keep notes compact and action-oriented.
                - Do not create follow-on packages in this stage.
                """
            ).strip()
        if stage == "mvp_scope":
            return dedent(
                """\
                - Define the smallest useful first release and the most important IT-admin user flows.
                - Decide whether this first release is viable now, viable with constraints, or blocked by missing input.
                - In stage_output, set verdict plus arrays for key_points, risks and open_questions.
                - Do not do broad web research. Prefer reasoning from the goal and acceptance criteria; if needed, use at most a few focused checks.
                - In notes, cover only the most decision-relevant rationale, not a long report.
                - The summary should clearly state the MVP feasibility verdict.
                - Keep the output short and concrete.
                - Do not create follow-on packages in this stage.
                """
            ).strip()
        if stage == "integration_feasibility":
            return dedent(
                """\
                - Evaluate only the highest-value SaaS integrations and identity/data sources for the MVP.
                - In stage_output, set verdict plus arrays for key_points, risks and open_questions.
                - Limit yourself to the few systems that matter most for a first release.
                - Prefer targeted checks over broad research and synthesize quickly.
                - The summary should state whether the first integrations are feasible for an MVP.
                - Do not create follow-on packages in this stage.
                """
            ).strip()
        if stage == "risk_review":
            return dedent(
                """\
                - Turn the current picture into a go, conditional or blocked planning decision.
                - In stage_output, set verdict plus arrays for key_points, risks and open_questions.
                - Focus on governance, security, operational safety and destructive offboarding risks.
                - Reuse earlier findings and avoid fresh broad research.
                - The summary should state the overall planning recommendation in one sentence.
                - Do not create follow-on packages in this stage.
                """
            ).strip()
        if stage == "architecture":
            return dedent(
                """\
                - Produce a practical implementation direction, not a generic essay.
                - In stage_output, fill components, decisions, delivery_sequence, validation_strategy and handoff.
                - Reuse information already collected in earlier stages and avoid wide new research unless essential.
                - In notes, keep only compact rationale for the most important tradeoffs.
                - The summary should state the recommended architecture in one sentence.
                - Do not create follow-on packages in this stage.
                """
            ).strip()
        if stage == "breakdown":
            return dedent(
                """\
                - Convert the architecture into concrete work packages that other agents can execute immediately.
                - In stage_output, fill generated_package_titles, generated_package_count and handoff.
                - Each new package should have a clear title, direct description, capability, and realistic priority.
                - Use depends_on keys when later packages must wait for earlier generated packages.
                - Prefer synthesis over more discovery. Build the package graph from the information already available.
                - Prefer a small, executable package graph over a large speculative backlog.
                """
            ).strip()
        if context["package"].get("kind") in {"implementation", "implementation-prep", "validation", "operations"}:
            return dedent(
                """\
                - Treat this as a focused implementation blueprint package, not as open-ended discovery.
                - Work primarily from the dependency summaries, stage outputs and acceptance criteria already present in the context.
                - Do not scan broad run history or search the whole workspace unless a single targeted lookup is strictly necessary.
                - Prefer a compact deliverable shape: scope, key design choices, flow/contract, failure handling, tests and handoff.
                - If the package is still too large to answer well, return status='blocked' with a precise split recommendation rather than timing out.
                - Keep the result concise and decision-ready.
                """
            ).strip()
        return "No stage-specific guidance."

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

    def _run_monitored_process(
        self,
        *,
        command: Sequence[str] | str,
        cwd: Path,
        env: Dict[str, str],
        input_text: Optional[str],
        shell: bool,
        agent_id: int,
        runner: Dict[str, Any],
    ) -> subprocess.CompletedProcess[str]:
        timeout_seconds = runner.get("timeout_seconds")
        heartbeat_interval = max(0.1, float(runner.get("heartbeat_interval_seconds", 30.0)))
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=shell,
            executable="/bin/zsh" if shell else None,
        )
        if input_text is not None and process.stdin is not None:
            process.stdin.write(input_text)
            process.stdin.close()
            # Prevent subprocess.communicate() from flushing a closed stdin pipe on timeout paths.
            process.stdin = None

        started_at = time.monotonic()
        while True:
            wait_timeout = heartbeat_interval
            if timeout_seconds is not None:
                elapsed = time.monotonic() - started_at
                remaining = float(timeout_seconds) - elapsed
                if remaining <= 0:
                    process.kill()
                    stdout_text, stderr_text = process.communicate()
                    raise RunnerTimeoutError(
                        timeout_seconds=float(timeout_seconds),
                        elapsed_seconds=elapsed,
                        return_code=process.returncode or -9,
                        stdout=stdout_text,
                        stderr=stderr_text,
                    )
                wait_timeout = min(wait_timeout, remaining)
            try:
                process.wait(timeout=wait_timeout)
                stdout_text = process.stdout.read() if process.stdout is not None else ""
                stderr_text = process.stderr.read() if process.stderr is not None else ""
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=process.returncode or 0,
                    stdout=stdout_text,
                    stderr=stderr_text,
                )
            except subprocess.TimeoutExpired:
                self.store.heartbeat(
                    agent_id,
                    status=AgentStatus.BUSY.value,
                    lease_seconds=self.orchestrator.lease_seconds,
                )

    def _run_shell_runner(
        self,
        agent_id: int,
        runner: Dict[str, Any],
        paths: Dict[str, Path],
        run_dir: Path,
    ) -> subprocess.CompletedProcess[str]:
        command = runner.get("command")
        if not command:
            raise ValueError("Shell runner requires a command.")
        formatted_command = self._format_shell_command(command, paths, run_dir)
        paths["command"].write_text(
            json.dumps(
                {
                    "runner_type": "shell",
                    "command": formatted_command,
                    "timeout_seconds": runner.get("timeout_seconds"),
                    "heartbeat_interval_seconds": runner.get("heartbeat_interval_seconds"),
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        completed = self._run_monitored_process(
            command=formatted_command,
            cwd=self._resolve_cwd(runner),
            env=self._shell_env(paths, run_dir),
            input_text=None,
            shell=True,
            agent_id=agent_id,
            runner=runner,
        )
        paths["stdout"].write_text(completed.stdout, encoding="utf-8")
        paths["stderr"].write_text(completed.stderr, encoding="utf-8")
        return completed

    def _run_codex_exec(
        self,
        agent_id: int,
        runner: Dict[str, Any],
        paths: Dict[str, Path],
    ) -> subprocess.CompletedProcess[str]:
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
            json.dumps(
                {
                    "runner_type": "codex_exec",
                    "command": command,
                    "timeout_seconds": runner.get("timeout_seconds"),
                    "heartbeat_interval_seconds": runner.get("heartbeat_interval_seconds"),
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        completed = self._run_monitored_process(
            command=command,
            cwd=self._resolve_cwd(runner),
            env=self._shell_env(paths, paths["command"].parent),
            input_text=paths["prompt"].read_text(encoding="utf-8"),
            shell=False,
            agent_id=agent_id,
            runner=runner,
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
        payload.setdefault("new_packages", [])
        payload.setdefault("stage_output", {})
        return payload

    def _validate_stage_output(self, package: Dict[str, Any], payload: Dict[str, Any]) -> None:
        stage = dict(package.get("metadata", {})).get("stage")
        if stage is None or payload.get("status") != "completed":
            return

        stage_output = dict(payload.get("stage_output") or {})

        def require_text_list(name: str) -> None:
            values = stage_output.get(name)
            if not isinstance(values, list) or not values or not all(isinstance(item, str) and item.strip() for item in values):
                raise ValueError(f"Stage '{stage}' requires stage_output.{name} with at least one non-empty string.")

        def require_text(name: str) -> None:
            value = stage_output.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Stage '{stage}' requires stage_output.{name} as a non-empty string.")

        if stage in {"mvp_scope", "integration_feasibility", "risk_review"}:
            if stage_output.get("verdict") not in {"go", "conditional", "blocked"}:
                raise ValueError(f"Stage '{stage}' requires stage_output.verdict as go, conditional or blocked.")
            require_text_list("key_points")
            require_text_list("risks")
            if "open_questions" not in stage_output or not isinstance(stage_output.get("open_questions"), list):
                raise ValueError(f"Stage '{stage}' requires stage_output.open_questions as an array.")
            return

        if stage == "architecture":
            require_text_list("components")
            require_text_list("decisions")
            require_text_list("delivery_sequence")
            require_text_list("validation_strategy")
            require_text("handoff")
            return

        if stage == "breakdown":
            if not payload.get("new_packages"):
                raise ValueError("Stage 'breakdown' must emit at least one new_packages entry.")
            require_text("handoff")
            require_text_list("generated_package_titles")
            count = stage_output.get("generated_package_count")
            if not isinstance(count, int) or count != len(payload["new_packages"]):
                raise ValueError("Stage 'breakdown' requires stage_output.generated_package_count to match new_packages.")

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
            "new_packages": payload.get("new_packages", []),
            "stage_output": payload.get("stage_output", {}),
            "return_code": return_code,
        }
        runs.append(run_record)
        metadata["runs"] = runs[-20:]
        metadata["latest_run"] = run_record
        if payload.get("stage_output"):
            metadata["stage_output"] = payload["stage_output"]
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

    def _runner_timeout_payload(self, error: RunnerTimeoutError, run_dir: Path, stderr_path: Path) -> Dict[str, Any]:
        error_excerpt = error.stderr.strip()
        if error_excerpt:
            error_excerpt = error_excerpt.splitlines()[-1]
        else:
            error_excerpt = "No stderr output captured."
        return {
            "status": "blocked",
            "summary": "Worker execution timed out",
            "blocker_reason": (
                f"Runner exceeded {error.timeout_seconds:.1f}s and was stopped after "
                f"{error.elapsed_seconds:.1f}s. See {run_dir}. Last stderr line: {error_excerpt}"
            ),
            "artifacts": [{"path": str(stderr_path), "description": "Runner stderr"}],
            "notes": [],
        }

    def run_agent_once(self, agent_name: str) -> Dict[str, Any]:
        agent = self.store.get_agent_by_name(agent_name)
        if agent is None:
            raise ValueError(f"Unknown agent '{agent_name}'")

        package = self.store.get_current_package_for_agent(agent["id"])
        current_status = AgentStatus.BUSY.value if package else AgentStatus.IDLE.value
        self.store.heartbeat(agent["id"], status=current_status, lease_seconds=self.orchestrator.lease_seconds)
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
        try:
            if runner["type"] == "shell":
                completed = self._run_shell_runner(agent["id"], runner, paths, run_dir)
            elif runner["type"] == "codex_exec":
                completed = self._run_codex_exec(agent["id"], runner, paths)
            else:
                raise ValueError(f"Unsupported runner type: {runner['type']}")
        except RunnerTimeoutError as exc:
            paths["stdout"].write_text(exc.stdout, encoding="utf-8")
            paths["stderr"].write_text(exc.stderr, encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=runner["type"],
                returncode=exc.return_code,
                stdout=exc.stdout,
                stderr=exc.stderr,
            )
            payload = self._runner_timeout_payload(exc, run_dir, paths["stderr"])
        else:
            if completed.returncode == 0:
                try:
                    payload = self._load_result_payload(paths["result"])
                    self._validate_stage_output(package, payload)
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

        created_package_ids: List[int] = []
        if payload["status"] == "completed":
            if payload.get("new_packages"):
                created_package_ids = self.orchestrator.add_packages(
                    goal_id=goal["id"],
                    packages=[
                        WorkPackageInput(
                            title=item["title"],
                            description=item["description"],
                            capability=item["capability"],
                            priority=int(item.get("priority", 50)),
                            kind=item.get("kind", "delivery"),
                            key=item.get("key"),
                            depends_on=list(item.get("depends_on", [])),
                            acceptance_criteria=list(item.get("acceptance_criteria", [])),
                            metadata=dict(item.get("metadata", {})),
                        )
                        for item in payload["new_packages"]
                    ],
                    parent_package_id=package["id"],
                    default_dependency_ids=[package["id"]],
                )
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
            "created_package_ids": created_package_ids,
            "run_dir": str(run_dir),
        }

    def run_cycle(
        self,
        goal_id: Optional[int] = None,
        agent_names: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        tick_result = self.orchestrator.tick()
        self.heartbeat_agents(agent_names=agent_names)
        worker_results: List[Dict[str, Any]] = []
        for agent in self._select_agents(agent_names):
            if agent["current_package_id"] is None:
                continue
            worker_results.append(self.run_agent_once(agent["name"]))
        dashboard = self.orchestrator.dashboard(goal_id=goal_id)
        return {
            "tick": tick_result,
            "worker_results": worker_results,
            "dashboard": dashboard,
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

    def run_service(
        self,
        poll_seconds: float = 5.0,
        max_cycles: Optional[int] = None,
        goal_id: Optional[int] = None,
        agent_names: Optional[Sequence[str]] = None,
        stop_when_idle: bool = False,
    ) -> Dict[str, Any]:
        cycles: List[Dict[str, Any]] = []
        target_goal_id = goal_id
        if target_goal_id is None:
            initial_dashboard = self.orchestrator.dashboard(goal_id=None)
            if initial_dashboard.get("goal"):
                target_goal_id = initial_dashboard["goal"]["id"]
        iteration = 0
        while True:
            iteration += 1
            cycle = self.run_cycle(goal_id=target_goal_id, agent_names=agent_names)
            dashboard = cycle["dashboard"]
            goal = dashboard.get("goal")
            cycles.append(
                {
                    "iteration": iteration,
                    "tick": cycle["tick"],
                    "worker_results": cycle["worker_results"],
                    "goal_status": goal["status"] if goal else None,
                }
            )

            has_activity = bool(
                cycle["tick"]["assignments"]
                or cycle["tick"]["resolution_packages"]
                or cycle["tick"]["requeued_packages"]
                or cycle["worker_results"]
            )
            goal_completed = bool(goal and goal["status"] == GoalStatus.COMPLETED.value)

            if goal_completed:
                break
            if max_cycles is not None and iteration >= max_cycles:
                break
            if stop_when_idle and not has_activity:
                break

            time.sleep(poll_seconds)

        final_dashboard = self.orchestrator.dashboard(goal_id=target_goal_id)
        return {
            "goal_id": target_goal_id,
            "cycles": cycles,
            "dashboard": final_dashboard,
        }
