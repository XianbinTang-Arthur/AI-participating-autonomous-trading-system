"""Workflow Dispatcher — 统一调度入口.

工作包 A: 将需要人工触发的关键流程纳入调度体系。

4 类 workflow:
  data_maintenance  — historical/realtime daemon, gold build, gap repair
  research_cycle    — calibration, research round, attribution, execution realism
  governance_cycle  — artifact validation, quality monitor, active rounds refresh
  decision_cycle    — decision round, pre-apply gate, observation, rollback eval

每个 workflow 从对应 JSON 配置加载任务列表，按顺序执行。
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

_WORKFLOW_CONFIG_DIR = "configs/rdp_workflows"


def _make_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


# ── 配置加载 ──────────────────────────────────────────────────────


def load_workflow_config(
    project_root: Path, workflow_name: str,
) -> dict[str, Any]:
    """加载 workflow 配置."""
    config_path = project_root / _WORKFLOW_CONFIG_DIR / f"{workflow_name}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Workflow 配置不存在: {config_path}")
    with config_path.open(encoding="utf-8") as f:
        return json.load(f)


def list_available_workflows(project_root: Path) -> list[str]:
    """列出所有可用的 workflow."""
    config_dir = project_root / _WORKFLOW_CONFIG_DIR
    if not config_dir.exists():
        return []
    return sorted(
        p.stem for p in config_dir.glob("*.json")
    )


# ── 任务执行 ──────────────────────────────────────────────────────


def _run_task(
    project_root: Path,
    task: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行单个任务."""
    task_name = task.get("name", "unknown")
    command = task.get("command", "")
    timeout = task.get("timeout_seconds", 300)
    allow_failure = task.get("allow_failure", False)

    result = {
        "name": task_name,
        "command": command,
        "status": "pending",
        "exit_code": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "allow_failure": allow_failure,
    }

    if not command:
        result["status"] = "skipped"
        result["error"] = "no command specified"
        return result

    # 将 command 拆成 argv list 并替换 python 为当前解释器绝对路径，
    # 避免 cron 等无 PATH 环境找不到 python。
    # 使用 shell=False + argv 代替 shell=True + shlex.quote，
    # 确保 Windows / POSIX 均能正确处理路径含空格的 venv。
    if command.startswith("python "):
        argv = [sys.executable, *shlex.split(command[7:])]
    else:
        argv = shlex.split(command)

    if dry_run:
        result["status"] = "dry_run"
        result["command"] = argv  # dry_run 时暴露实际 argv 便于诊断
        return result

    result["started_at"] = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result["exit_code"] = proc.returncode
        result["status"] = "success" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            # 取最后 500 字符的 stderr
            result["error"] = (proc.stderr or "")[-500:].strip()
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = f"超时 ({timeout}s)"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:500]

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ── Workflow 执行 ──────────────────────────────────────────────────


def run_workflow(
    project_root: Path,
    workflow_name: str,
    *,
    dry_run: bool = False,
    stop_on_failure: bool = True,
) -> dict[str, Any]:
    """执行指定 workflow 的所有任务.

    Returns
    -------
    dict  workflow 执行报告:
      - run_id, workflow, started_at, finished_at
      - tasks: list[task_result]
      - overall_status: "success" / "partial" / "failed"
    """
    config = load_workflow_config(project_root, workflow_name)
    tasks = config.get("tasks", [])
    run_id = _make_run_id()

    report: dict[str, Any] = {
        "run_id": run_id,
        "workflow": workflow_name,
        "description": config.get("description", ""),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "task_count": len(tasks),
        "tasks": [],
        "overall_status": "pending",
    }

    succeeded = 0
    failed = 0
    skipped = 0

    for task in tasks:
        enabled = task.get("enabled", True)
        if not enabled:
            report["tasks"].append({
                "name": task.get("name", "?"),
                "status": "disabled",
            })
            skipped += 1
            continue

        task_result = _run_task(project_root, task, dry_run=dry_run)
        report["tasks"].append(task_result)

        if task_result["status"] == "success" or task_result["status"] == "dry_run":
            succeeded += 1
        elif task_result["status"] in ("failed", "timeout", "error"):
            failed += 1
            if stop_on_failure and not task_result.get("allow_failure"):
                # 后续任务标记为 skipped
                remaining = tasks[tasks.index(task) + 1:]
                for rt in remaining:
                    report["tasks"].append({
                        "name": rt.get("name", "?"),
                        "status": "skipped_due_to_failure",
                    })
                    skipped += 1
                break
        else:
            skipped += 1

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["succeeded"] = succeeded
    report["failed"] = failed
    report["skipped"] = skipped

    if failed == 0:
        report["overall_status"] = "success"
    elif succeeded > 0:
        report["overall_status"] = "partial"
    else:
        report["overall_status"] = "failed"

    # 保存执行报告
    _save_run_report(project_root, report)

    return report


def _save_run_report(project_root: Path, report: dict[str, Any]) -> Path:
    """保存 workflow 执行报告."""
    log_dir = project_root / "artifacts/operations/workflow_runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{report['run_id']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    return path
