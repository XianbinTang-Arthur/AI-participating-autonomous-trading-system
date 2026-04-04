"""Workflow 失败记录注册表.

工作包 B: 记录 workflow 执行失败，供后续分析和补跑使用。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON (tmpfile → fsync → replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _failures_path(root: Path) -> Path:
    return root / "artifacts" / "operations" / "workflow_failures.json"


def load_failures(root: Path) -> dict:
    """加载失败记录."""
    fp = _failures_path(root)
    if not fp.exists():
        return {"failures": [], "generated_at": None}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_failures(root: Path, data: dict) -> Path:
    """保存失败记录."""
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    fp = _failures_path(root)
    _atomic_write_json(fp, data)
    return fp


def record_failure(
    root: Path,
    *,
    workflow: str,
    run_id: str,
    task_name: str,
    error_message: str,
    exit_code: int | None = None,
    run_report_path: str | None = None,
    notes: str = "",
) -> dict:
    """记录一次 workflow 任务失败.

    Returns:
        failure record dict
    """
    now = datetime.now(timezone.utc)
    failure_id = f"fail_{workflow}_{task_name}_{now.strftime('%Y%m%d_%H%M%S')}"

    record = {
        "failure_id": failure_id,
        "workflow": workflow,
        "run_id": run_id,
        "task_name": task_name,
        "error_message": error_message,
        "exit_code": exit_code,
        "run_report_path": run_report_path,
        "recorded_at": now.isoformat(),
        "status": "open",  # open / retried / resolved / ignored
        "retry_count": 0,
        "last_retry_at": None,
        "last_retry_result": None,
        "resolution_notes": notes,
    }

    data = load_failures(root)
    data["failures"].append(record)
    save_failures(root, data)
    return record


def find_failure(root: Path, failure_id: str) -> dict | None:
    """按 failure_id 查找失败记录."""
    data = load_failures(root)
    for f in data["failures"]:
        if f.get("failure_id") == failure_id:
            return f
    return None


def list_open_failures(root: Path) -> list[dict]:
    """列出所有 open 状态的失败记录."""
    data = load_failures(root)
    return [f for f in data["failures"] if f.get("status") == "open"]


def update_failure_status(
    root: Path,
    failure_id: str,
    *,
    status: str,
    notes: str = "",
) -> dict | None:
    """更新失败记录状态."""
    data = load_failures(root)
    for f in data["failures"]:
        if f.get("failure_id") == failure_id:
            f["status"] = status
            if notes:
                f["resolution_notes"] = notes
            save_failures(root, data)
            return f
    return None


def record_retry_attempt(
    root: Path,
    failure_id: str,
    *,
    success: bool,
    detail: str = "",
) -> dict | None:
    """记录一次补跑尝试."""
    data = load_failures(root)
    now = datetime.now(timezone.utc)
    for f in data["failures"]:
        if f.get("failure_id") == failure_id:
            f["retry_count"] = f.get("retry_count", 0) + 1
            f["last_retry_at"] = now.isoformat()
            f["last_retry_result"] = "success" if success else "failed"
            if success:
                f["status"] = "retried"
            if detail:
                f["resolution_notes"] = detail
            save_failures(root, data)
            return f
    return None
