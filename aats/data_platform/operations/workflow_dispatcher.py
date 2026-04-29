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
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import try_governance_db

log = logging.getLogger(__name__)

_WORKFLOW_CONFIG_DIR = "configs/rdp_workflows"


def _make_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _tail_text(text: str, *, lines: int = 20, max_chars: int = 2400) -> str:
    if not text:
        return ""
    text = text[-max_chars:]
    tail = text.splitlines()[-lines:]
    return "\n".join(tail).strip()


def _missing_success_markers(text: str, markers: list[str]) -> list[str]:
    if not markers:
        return []
    return [marker for marker in markers if marker not in text]


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


def _is_task_enabled(task: dict[str, Any]) -> bool:
    return task.get("enabled", True) is not False


def describe_manual_trigger_availability(
    project_root: Path,
    workflow_name: str,
) -> dict[str, Any]:
    """Return whether an operator-triggered workflow matches executable config.

    This guard is intentionally narrower than daemon execution. The daemon may
    still process scheduler-created maintenance workflows, but an operator UI
    button must not advertise "运行完整 RDP" when the configured full pipeline is
    frozen and would be skipped.
    """
    try:
        config = load_workflow_config(project_root, workflow_name)
    except FileNotFoundError:
        return {
            "enabled": False,
            "disabled_reason": f"{workflow_name} 的 workflow 配置不存在，不能手动触发。",
            "enabled_task_names": [],
        }
    except Exception:
        log.exception("Failed to inspect workflow manual trigger availability: %s", workflow_name)
        return {
            "enabled": False,
            "disabled_reason": f"{workflow_name} 的 workflow 配置暂时不可读取，不能手动触发。",
            "enabled_task_names": [],
        }

    tasks = [
        task for task in (config.get("tasks") or [])
        if isinstance(task, dict)
    ]
    enabled_task_names = [
        str(task.get("name") or "")
        for task in tasks
        if _is_task_enabled(task) and str(task.get("name") or "").strip()
    ]

    if workflow_name == "research_cycle":
        full_pipeline_task = next(
            (task for task in tasks if task.get("name") == "full_pipeline"),
            None,
        )
        if full_pipeline_task is None:
            return {
                "enabled": False,
                "disabled_reason": "完整 RDP 缺少 full_pipeline 任务配置，不能手动触发。",
                "enabled_task_names": enabled_task_names,
            }
        if not _is_task_enabled(full_pipeline_task):
            return {
                "enabled": False,
                "disabled_reason": (
                    "完整 RDP 当前被冻结，full_pipeline 任务已禁用；"
                    "提交 research_cycle 只会刷新数据，不会运行研究闭环。"
                    "请先使用“刷新数据”。"
                ),
                "enabled_task_names": enabled_task_names,
            }

    if not enabled_task_names:
        return {
            "enabled": False,
            "disabled_reason": f"{workflow_name} 当前没有启用的任务，不能手动触发。",
            "enabled_task_names": [],
        }

    return {
        "enabled": True,
        "disabled_reason": None,
        "enabled_task_names": enabled_task_names,
    }


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
    success_markers = [
        str(marker) for marker in task.get("success_markers", [])
        if str(marker).strip()
    ]

    result = {
        "name": task_name,
        "command": command,
        "status": "pending",
        "exit_code": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "output_tail": None,
        "stdout_tail": None,
        "stderr_tail": None,
        "allow_failure": allow_failure,
        "success_markers": success_markers,
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
        argv = [sys.executable, *shlex.split(command[7:], posix=os.name != "nt")]
    else:
        argv = shlex.split(command, posix=os.name != "nt")

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
        stdout_tail = _tail_text(proc.stdout or "")
        stderr_tail = _tail_text(proc.stderr or "")
        combined_output = "\n".join(
            part for part in (proc.stdout or "", proc.stderr or "") if part
        )
        combined_tail = _tail_text(combined_output)
        result["stdout_tail"] = stdout_tail or None
        result["stderr_tail"] = stderr_tail or None
        result["output_tail"] = combined_tail or None
        if proc.returncode != 0:
            # 取最后 500 字符的 stderr
            result["error"] = (proc.stderr or "")[-500:].strip()
        elif success_markers:
            missing_markers = _missing_success_markers(combined_output, success_markers)
            if missing_markers:
                result["status"] = "failed"
                result["error"] = "missing success markers: " + ", ".join(missing_markers)
                result["missing_success_markers"] = missing_markers
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
    failed_but_allowed = 0
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
            if task_result.get("allow_failure"):
                # allow_failure=true 的 task 失败视为 degraded 而非硬失败：
                # workflow 整体可以继续推进，overall_status 不降为 failed。
                # RDP Bug 1/8 场景：observation_cycle 遇到 rollback 被拒
                # (deprecated target) 等业务问题时，不应阻塞 hourly observation
                # 推进节奏，通过 Bug 6 structured log 暴露给 operator 人工处理。
                failed_but_allowed += 1
            else:
                failed += 1
                if stop_on_failure:
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
    report["failed_but_allowed"] = failed_but_allowed
    report["skipped"] = skipped

    if failed == 0 and failed_but_allowed == 0:
        report["overall_status"] = "success"
    elif failed == 0 and failed_but_allowed > 0:
        # 所有失败都是 allow_failure task，视为 degraded 但不是硬失败
        report["overall_status"] = "degraded"
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
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_upsert_workflow_run_report,
            )

            with Session(engine) as session, session.begin():
                db_upsert_workflow_run_report(session, report)
        except Exception as exc:
            log.warning("workflow run report DB 同步失败: %s", exc)
        finally:
            if engine is not None:
                engine.dispose()
    return path
