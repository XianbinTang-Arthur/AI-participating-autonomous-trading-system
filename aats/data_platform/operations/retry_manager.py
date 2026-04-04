"""Workflow 补跑管理器.

工作包 B: 对失败的 workflow 任务进行补跑，支持单任务补跑和整 workflow 补跑。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aats.data_platform.operations.failure_registry import (
    find_failure,
    load_failures,
    record_retry_attempt,
)
from aats.data_platform.operations.workflow_dispatcher import (
    load_workflow_config,
    run_workflow,
)


def retry_single_task(
    root: Path,
    failure_id: str,
    *,
    timeout_override: int | None = None,
    dry_run: bool = False,
) -> dict:
    """补跑单个失败任务.

    从失败记录中提取 workflow 和 task 信息，找到对应命令重新执行。

    Returns:
        {"success": bool, "failure_id": str, "detail": str, ...}
    """
    failure = find_failure(root, failure_id)
    if failure is None:
        return {
            "success": False,
            "failure_id": failure_id,
            "detail": f"failure_id {failure_id} not found",
        }

    if failure.get("status") not in ("open", "retried"):
        return {
            "success": False,
            "failure_id": failure_id,
            "detail": f"failure status is '{failure.get('status')}', not retriable",
        }

    workflow_name = failure["workflow"]
    task_name = failure["task_name"]

    # 加载 workflow 配置找到命令
    try:
        config = load_workflow_config(root, workflow_name)
    except FileNotFoundError as e:
        return {
            "success": False,
            "failure_id": failure_id,
            "detail": f"workflow config not found: {e}",
        }

    task_config = None
    for t in config.get("tasks", []):
        if t.get("name") == task_name:
            task_config = t
            break

    if task_config is None:
        return {
            "success": False,
            "failure_id": failure_id,
            "detail": f"task '{task_name}' not found in workflow '{workflow_name}'",
        }

    command = task_config["command"]
    timeout = timeout_override or task_config.get("timeout_seconds", 120)

    if dry_run:
        return {
            "success": True,
            "failure_id": failure_id,
            "detail": f"[DRY RUN] would execute: {command} (timeout={timeout}s)",
            "dry_run": True,
        }

    # 实际执行
    now = datetime.now(timezone.utc)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
        )
        success = result.returncode == 0
        detail = (
            f"exit_code={result.returncode}, "
            f"stdout={result.stdout[:200] if result.stdout else ''}, "
            f"stderr={result.stderr[:200] if result.stderr else ''}"
        )
    except subprocess.TimeoutExpired:
        success = False
        detail = f"timeout after {timeout}s"
    except Exception as e:
        success = False
        detail = f"error: {e}"

    # 记录补跑结果
    record_retry_attempt(
        root, failure_id, success=success, detail=detail
    )

    return {
        "success": success,
        "failure_id": failure_id,
        "task_name": task_name,
        "workflow": workflow_name,
        "command": command,
        "detail": detail,
        "retried_at": now.isoformat(),
    }


def retry_workflow(
    root: Path,
    failure_id: str,
    *,
    dry_run: bool = False,
    stop_on_failure: bool = True,
) -> dict:
    """补跑失败记录对应的整个 workflow.

    Returns:
        workflow run report
    """
    failure = find_failure(root, failure_id)
    if failure is None:
        return {
            "success": False,
            "detail": f"failure_id {failure_id} not found",
        }

    workflow_name = failure["workflow"]

    if dry_run:
        return {
            "success": True,
            "detail": f"[DRY RUN] would re-run workflow '{workflow_name}'",
            "dry_run": True,
        }

    report = run_workflow(
        root,
        workflow_name,
        dry_run=False,
        stop_on_failure=stop_on_failure,
    )

    success = report.get("overall_status") == "success"
    record_retry_attempt(
        root,
        failure_id,
        success=success,
        detail=f"full workflow retry, status={report.get('overall_status')}",
    )

    return {
        "success": success,
        "failure_id": failure_id,
        "workflow": workflow_name,
        "report": report,
    }


def auto_record_failures_from_report(
    root: Path,
    report: dict,
) -> list[dict]:
    """从 workflow 运行报告中自动提取失败任务并记录.

    Returns:
        list of recorded failure records
    """
    from aats.data_platform.operations.failure_registry import record_failure

    recorded = []
    workflow = report.get("workflow", "unknown")
    run_id = report.get("run_id", "unknown")

    for task in report.get("tasks", []):
        status = task.get("status", "")
        if status in ("failed", "error", "timeout"):
            rec = record_failure(
                root,
                workflow=workflow,
                run_id=run_id,
                task_name=task.get("name", "unknown"),
                error_message=task.get("error", f"status={status}"),
                exit_code=task.get("exit_code"),
            )
            recorded.append(rec)

    return recorded
