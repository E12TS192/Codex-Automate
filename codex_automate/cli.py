from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from codex_automate.database import resolve_database_target
from codex_automate.orchestrator import Orchestrator
from codex_automate.runtime import WorkerRuntime
from codex_automate.simulation import run_demo
from codex_automate.state import StateStore

DB_HELP = (
    "SQLite path or database URL. Defaults to CODEX_AUTOMATE_DATABASE_URL, DATABASE_URL, "
    "POSTGRES_URL, then state/codex_automate.sqlite3."
)


def add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=None, help=DB_HELP)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Automate prototype")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Create the SQLite schema.")
    add_database_argument(bootstrap_parser)

    register_parser = subparsers.add_parser("register-agent", help="Register or update an agent.")
    add_database_argument(register_parser)
    register_parser.add_argument("--name", required=True, help="Agent name")
    register_parser.add_argument(
        "--capability",
        dest="capabilities",
        action="append",
        required=True,
        help="Capability exposed by this agent. Repeatable.",
    )
    register_parser.add_argument(
        "--runner-type",
        choices=["codex_exec", "shell"],
        default="codex_exec",
        help="Worker runner used by this agent.",
    )
    register_parser.add_argument(
        "--command",
        dest="runner_command",
        help="Shell command for runner type 'shell'.",
    )
    register_parser.add_argument("--cwd", help="Working directory for the runner.")
    register_parser.add_argument("--model", help="Codex model for runner type 'codex_exec'.")
    register_parser.add_argument(
        "--sandbox",
        default="workspace-write",
        help="Sandbox mode for runner type 'codex_exec'.",
    )
    register_parser.add_argument(
        "--instruction",
        action="append",
        default=[],
        help="Additional instruction injected into the worker prompt. Repeatable.",
    )
    register_parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Additional writable directory for the worker. Repeatable.",
    )
    register_parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="Hard timeout for one worker run. Defaults to the orchestrator lease length.",
    )
    register_parser.add_argument(
        "--heartbeat-interval-seconds",
        type=float,
        help="Heartbeat cadence while a worker process is running.",
    )

    goal_parser = subparsers.add_parser("submit-goal", help="Submit a goal from a JSON file.")
    add_database_argument(goal_parser)
    goal_parser.add_argument("--file", required=True, help="Path to goal JSON")

    tick_parser = subparsers.add_parser("tick", help="Run one orchestrator control-loop tick.")
    add_database_argument(tick_parser)

    run_agent_parser = subparsers.add_parser("run-agent", help="Run the currently assigned package for one agent.")
    add_database_argument(run_agent_parser)
    run_agent_parser.add_argument("--name", required=True, help="Agent name")
    run_agent_parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Workspace root used for run artifacts and prompts.",
    )

    service_parser = subparsers.add_parser("serve-workers", help="Run a poll loop for the worker host.")
    add_database_argument(service_parser)
    service_parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Workspace root used for run artifacts and prompts.",
    )
    service_parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Sleep interval between worker cycles.",
    )
    service_parser.add_argument(
        "--max-cycles",
        type=int,
        help="Optional upper bound for service cycles.",
    )
    service_parser.add_argument(
        "--goal-id",
        type=int,
        help="Restrict the service view to one goal.",
    )
    service_parser.add_argument(
        "--agent",
        action="append",
        default=[],
        help="Only manage the given agent names. Repeatable.",
    )
    service_parser.add_argument(
        "--stop-when-idle",
        action="store_true",
        help="Exit when one cycle completes without assignments or worker runs.",
    )

    autopilot_parser = subparsers.add_parser("autopilot", help="Run orchestrator + workers until stable or complete.")
    add_database_argument(autopilot_parser)
    autopilot_parser.add_argument("--goal-id", type=int, help="Goal to run. Defaults to the latest goal.")
    autopilot_parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Workspace root used for run artifacts and prompts.",
    )
    autopilot_parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum autopilot iterations before stopping.",
    )

    dashboard_parser = subparsers.add_parser("dashboard", help="Print the current system state.")
    add_database_argument(dashboard_parser)
    dashboard_parser.add_argument("--goal-id", type=int, help="Restrict output to one goal.")

    demo_parser = subparsers.add_parser("demo", help="Run a built-in end-to-end simulation.")
    add_database_argument(demo_parser)
    demo_parser.add_argument("--reset", action="store_true", help="Delete the demo database first.")
    demo_parser.add_argument(
        "--max-steps",
        type=int,
        default=12,
        help="Maximum number of control-loop iterations.",
    )

    return parser


def _print_heading(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def _print_kv(items: Iterable[tuple[str, Any]]) -> None:
    for key, value in items:
        print(f"{key}: {value}")


def _print_dashboard(payload: Dict[str, Any]) -> None:
    goal = payload.get("goal")
    goals = payload.get("goals", [])
    agents = payload.get("agents", [])
    packages = payload.get("packages", [])
    events = payload.get("events", [])

    if goal:
        _print_heading("Goal")
        _print_kv(
            [
                ("id", goal["id"]),
                ("title", goal["title"]),
                ("status", goal["status"]),
                ("objective", goal["objective"]),
            ]
        )
    elif goals:
        _print_heading("Goals")
        for item in goals:
            print(f"[{item['id']}] {item['title']} ({item['status']})")

    _print_heading("Agents")
    if not agents:
        print("No agents registered.")
    for agent in agents:
        capabilities = ", ".join(agent["capabilities"])
        current = agent["current_package_id"] or "-"
        print(
            f"[{agent['id']}] {agent['name']} | {agent['status']} | caps={capabilities} | current={current}"
        )

    _print_heading("Packages")
    if not packages:
        print("No packages visible.")
    for package in packages:
        blocker = f" | blocker={package['blocker_reason']}" if package["blocker_reason"] else ""
        deps = ",".join(str(dep) for dep in package["dependency_ids"]) or "-"
        print(
            f"[{package['id']}] {package['title']} | {package['status']} | cap={package['capability']} | "
            f"deps={deps} | priority={package['priority']}{blocker}"
        )

    _print_heading("Recent Events")
    if not events:
        print("No events recorded.")
    for event in events:
        print(
            f"[{event['id']}] {event['created_at']} | {event['event_type']} | "
            f"{event['entity_type']}:{event['entity_id']} | payload={json.dumps(event['payload'], ensure_ascii=True)}"
        )


def _load_goal(path: str) -> Dict[str, Any]:
    content = Path(path).read_text(encoding="utf-8")
    return json.loads(content)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    database_target = resolve_database_target(args.db)
    store = StateStore(database_target)
    store.initialize()
    orchestrator = Orchestrator(store)

    if args.command == "bootstrap":
        print(f"Initialized state database at {database_target}")
        return 0

    if args.command == "register-agent":
        if args.runner_type == "shell" and not args.runner_command:
            parser.error("--command is required when --runner-type shell is used.")
        runner: Dict[str, Any] = {
            "type": args.runner_type,
            "sandbox": args.sandbox,
            "instructions": list(args.instruction or []),
            "add_dirs": [str(Path(path).resolve()) for path in (args.add_dir or [])],
        }
        if args.runner_command:
            runner["command"] = args.runner_command
        if args.cwd:
            runner["cwd"] = str(Path(args.cwd).resolve())
        if args.model:
            runner["model"] = args.model
        if args.timeout_seconds is not None:
            runner["timeout_seconds"] = args.timeout_seconds
        if args.heartbeat_interval_seconds is not None:
            runner["heartbeat_interval_seconds"] = args.heartbeat_interval_seconds
        metadata = {"runner": runner}
        agent_id = store.register_agent(args.name, args.capabilities, metadata=metadata)
        print(f"Registered agent {args.name} as #{agent_id}")
        return 0

    if args.command == "submit-goal":
        goal_payload = _load_goal(args.file)
        goal_id = orchestrator.submit_goal_from_dict(goal_payload)
        print(f"Submitted goal #{goal_id}: {goal_payload['title']}")
        return 0

    if args.command == "tick":
        result = orchestrator.tick()
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0

    if args.command == "run-agent":
        runtime = WorkerRuntime(store=store, workspace_root=args.workspace, orchestrator=orchestrator)
        result = runtime.run_agent_once(args.name)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0

    if args.command == "serve-workers":
        runtime = WorkerRuntime(store=store, workspace_root=args.workspace, orchestrator=orchestrator)
        result = runtime.run_service(
            goal_id=args.goal_id,
            poll_seconds=args.poll_seconds,
            max_cycles=args.max_cycles,
            agent_names=args.agent or None,
            stop_when_idle=args.stop_when_idle,
        )
        dashboard = result["dashboard"]
        if dashboard.get("goal"):
            print(
                f"Worker service stopped with goal #{dashboard['goal']['id']} "
                f"in status {dashboard['goal']['status']}"
            )
        else:
            print("Worker service stopped without an active goal selection")
        _print_dashboard(dashboard)
        _print_heading("Cycles")
        for item in result["cycles"]:
            print(
                f"cycle={item['iteration']} status={item['goal_status']} "
                f"assignments={len(item['tick']['assignments'])} "
                f"worker_runs={len(item['worker_results'])}"
            )
            for worker_result in item["worker_results"]:
                print(
                    f"  - {worker_result['agent_name']} -> {worker_result['outcome']} "
                    f"({worker_result['summary']})"
                )
        return 0

    if args.command == "autopilot":
        runtime = WorkerRuntime(store=store, workspace_root=args.workspace, orchestrator=orchestrator)
        result = runtime.run_autopilot(goal_id=args.goal_id, max_iterations=args.max_iterations)
        dashboard = result["dashboard"]
        if dashboard.get("goal"):
            print(
                f"Autopilot stopped with goal #{dashboard['goal']['id']} "
                f"in status {dashboard['goal']['status']}"
            )
        _print_dashboard(dashboard)
        _print_heading("Timeline")
        for item in result["timeline"]:
            print(
                f"iteration={item['iteration']} status={item['goal_status']} "
                f"assignments={len(item['tick']['assignments'])} "
                f"worker_runs={len(item['worker_results'])}"
            )
            for worker_result in item["worker_results"]:
                print(
                    f"  - {worker_result['agent_name']} -> {worker_result['outcome']} "
                    f"({worker_result['summary']})"
                )
        return 0

    if args.command == "dashboard":
        payload = orchestrator.dashboard(goal_id=args.goal_id)
        _print_dashboard(payload)
        return 0

    if args.command == "demo":
        demo_target = database_target
        result = run_demo(demo_target, reset=args.reset, max_steps=args.max_steps)
        print(f"Demo goal #{result['goal_id']} completed with status {result['dashboard']['goal']['status']}")
        _print_dashboard(result["dashboard"])
        _print_heading("Timeline")
        for item in result["timeline"]:
            print(
                f"step={item['step']} status={item['goal_status']} "
                f"assignments={len(item['tick']['assignments'])} "
                f"actions={len(item['worker_actions'])}"
            )
            for action in item["worker_actions"]:
                print(f"  - {action}")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 1
