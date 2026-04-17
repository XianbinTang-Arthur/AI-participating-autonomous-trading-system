#!/usr/bin/env python3
"""RDP Task Daemon — 宿主机侧轮询 governance.rdp_task_queue 并执行 workflow.

在 WSL2 宿主机上运行，桥接 Docker 容器内 Gateway UI 的任务触发和实际脚本执行。

用法:
    # 前台运行（开发调试）
    python scripts/rdp_task_daemon.py

    # 后台运行
    nohup python scripts/rdp_task_daemon.py &

    # 指定轮询间隔（默认 10 秒）
    python scripts/rdp_task_daemon.py --poll-interval 5

    # 单次执行（不循环，适合 cron）
    python scripts/rdp_task_daemon.py --once

连接串:
    复用 RDP_DATABASE_URL (.env.research) 或 AATS_ACTIVE_PARAMETER_DB_URL 环境变量。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_task_daemon")

# 每个 workflow 的超时时间（秒）
# Fix P1: 显式列出所有 workflow 的超时，避免依赖 DEFAULT_TIMEOUT 掩盖遗漏
WORKFLOW_TIMEOUTS = {
    "data_maintenance": 900,   # 15 分钟
    "research_cycle": 3600,    # 60 分钟
    "decision_cycle": 1800,    # 30 分钟 — 包含参数评估和可能的实盘回滚
    "governance_cycle": 1800,  # 30 分钟 — 治理决策评估
    "release_cycle": 900,      # 15 分钟 — approved recommendation -> release/apply
}
DEFAULT_TIMEOUT = 1800  # 30 分钟

# 保留最后 N 行日志
LOG_TAIL_LINES = 50
HEARTBEAT_INTERVAL_SECONDS = 5
LOCAL_HEARTBEAT_PATH = Path("/tmp/rdp_daemon_heartbeat.json")
RUNTIME_COMPONENT = "rdp-daemon"

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %s, shutting down after current task...", signum)
    _shutdown = True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RDP Task Queue Daemon")
    p.add_argument("--poll-interval", type=int, default=10,
                   help="Seconds between polls when idle (default: 10)")
    p.add_argument("--once", action="store_true",
                   help="Process one task (if any) and exit")
    p.add_argument(
        "--enable-scheduler",
        action="store_true",
        help="Evaluate workflow schedules and enqueue due tasks before polling",
    )
    return p.parse_args()


def tail_lines(text: str, n: int) -> str:
    """取最后 n 行."""
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_local_heartbeat(payload: dict[str, object]) -> None:
    try:
        LOCAL_HEARTBEAT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _publish_heartbeat(
    *,
    status: str,
    poll_interval: int,
    active_task: dict[str, object] | None = None,
    last_task: dict[str, object] | None = None,
    error_message: str | None = None,
) -> None:
    heartbeat_at = _utcnow()
    payload: dict[str, object] = {
        "component": RUNTIME_COMPONENT,
        "status": status,
        "heartbeat_at": heartbeat_at.isoformat(),
        "pid": os.getpid(),
        "poll_interval_seconds": poll_interval,
    }
    if active_task:
        payload["active_task"] = active_task
    if last_task:
        payload["last_task"] = last_task
    if error_message:
        payload["error_message"] = error_message

    _write_local_heartbeat(payload)

    try:
        from aats.data_platform.db import get_session
        from aats.data_platform.governance.rdp_runtime_status_db import (
            db_upsert_runtime_status,
        )

        with get_session() as session:
            db_upsert_runtime_status(
                session,
                component=RUNTIME_COMPONENT,
                status=status,
                heartbeat_at=heartbeat_at,
                details=payload,
            )
    except Exception:
        log.exception("Failed to publish daemon heartbeat")


def execute_workflow(
    workflow: str,
    *,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, str, str]:
    """执行一个 workflow，返回 (exit_code, stdout_tail, error_message)."""
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "scripts" / "rdp_run_scheduled_workflow.py"),
        "--workflow", workflow,
    ]
    timeout = WORKFLOW_TIMEOUTS.get(workflow, DEFAULT_TIMEOUT)

    log.info("Executing: %s (timeout=%ds)", " ".join(cmd), timeout)
    try:
        with tempfile.SpooledTemporaryFile(
            mode="w+t",
            encoding="utf-8",
            max_size=1024 * 1024,
        ) as output_file:
            proc = subprocess.Popen(
                cmd,
                stdout=output_file,
                stderr=output_file,
                text=True,
                cwd=str(_PROJECT_ROOT),
            )
            start = time.monotonic()
            while True:
                exit_code = proc.poll()
                if exit_code is not None:
                    output_file.flush()
                    output_file.seek(0)
                    combined = output_file.read()
                    return exit_code, tail_lines(combined, LOG_TAIL_LINES), ""
                if time.monotonic() - start >= timeout:
                    proc.kill()
                    proc.wait(timeout=5)
                    output_file.flush()
                    output_file.seek(0)
                    combined = output_file.read()
                    return -1, tail_lines(combined, LOG_TAIL_LINES), f"Timeout after {timeout}s"
                if on_progress is not None:
                    on_progress()
                time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    except Exception as exc:
        return -2, "", str(exc)


def _recover_orphaned_running_tasks() -> list[dict[str, object]]:
    from aats.data_platform.db import get_session
    from aats.data_platform.governance.rdp_task_db import (
        db_recover_orphaned_running_tasks,
    )

    with get_session() as session:
        recovered = db_recover_orphaned_running_tasks(
            session,
            error_message="rdp_daemon_restarted_before_task_finished",
            exit_code=-3,
        )
    if recovered:
        log.warning(
            "Recovered %d orphaned running tasks after daemon startup: %s",
            len(recovered),
            ", ".join(str(item.get("task_id")) for item in recovered),
        )
    return recovered


def process_one_task(*, poll_interval: int) -> dict[str, object]:
    """尝试领取并执行一个任务."""
    from aats.data_platform.db import get_session
    from aats.data_platform.governance.rdp_task_db import (
        db_claim_next_task,
        db_update_task_status,
    )

    # claim（在一个事务里 SELECT FOR UPDATE + UPDATE running）
    with get_session() as session:
        task = db_claim_next_task(session)

    if task is None:
        return {"processed": False, "status": "idle"}

    task_id = task["task_id"]
    workflow = task["workflow"]
    log.info("=== Processing task %s: workflow=%s ===", task_id, workflow)

    active_task = {
        "task_id": task_id,
        "workflow": workflow,
        "status": "running",
        "started_at": _utcnow().isoformat(),
    }
    _publish_heartbeat(
        status="busy",
        poll_interval=poll_interval,
        active_task=active_task,
        last_task=active_task,
    )

    exit_code, log_tail, error_message = execute_workflow(
        workflow,
        on_progress=lambda: _publish_heartbeat(
            status="busy",
            poll_interval=poll_interval,
            active_task=active_task,
            last_task=active_task,
        ),
    )

    status = "done" if exit_code == 0 else "failed"
    if error_message == "" and exit_code != 0:
        error_message = f"Process exited with code {exit_code}"

    with get_session() as session:
        db_update_task_status(
            session, task_id,
            status=status,
            exit_code=exit_code,
            error_message=error_message or None,
            log_tail=log_tail or None,
        )

    log.info("=== Task %s finished: %s (exit=%s) ===", task_id, status, exit_code)
    return {
        "processed": True,
        "task_id": task_id,
        "workflow": workflow,
        "status": status,
        "exit_code": exit_code,
        "error_message": error_message or None,
        "finished_at": _utcnow().isoformat(),
    }


def _run_scheduler_once() -> None:
    from aats.data_platform.operations.workflow_scheduler import enqueue_due_workflows

    report = enqueue_due_workflows(_PROJECT_ROOT, actor="scheduler")
    if report.get("errors"):
        log.warning("Scheduler reported errors: %s", report["errors"])


def main() -> int:
    args = parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    from aats.data_platform.db import run_migrations

    run_migrations()
    _recover_orphaned_running_tasks()

    log.info("RDP Task Daemon started (poll_interval=%ds, once=%s)",
             args.poll_interval, args.once)
    _publish_heartbeat(status="starting", poll_interval=args.poll_interval)

    if args.once:
        if args.enable_scheduler:
            _run_scheduler_once()
        processed = process_one_task(poll_interval=args.poll_interval)
        _publish_heartbeat(
            status="idle" if not processed.get("processed") else str(processed.get("status")),
            poll_interval=args.poll_interval,
            last_task=processed if processed.get("processed") else None,
            error_message=(
                str(processed.get("error_message"))
                if processed.get("error_message")
                else None
            ),
        )
        if not processed.get("processed"):
            log.info("No pending tasks.")
        return 0

    last_task: dict[str, object] | None = None
    while not _shutdown:
        try:
            if args.enable_scheduler:
                _run_scheduler_once()
            processed = process_one_task(poll_interval=args.poll_interval)
            if processed.get("processed"):
                last_task = processed
            heartbeat_status = "idle"
            if processed.get("processed"):
                heartbeat_status = (
                    "healthy"
                    if processed.get("status") == "done"
                    else "degraded"
                )
            _publish_heartbeat(
                status=heartbeat_status,
                poll_interval=args.poll_interval,
                last_task=last_task,
                error_message=(
                    str(processed.get("error_message"))
                    if processed.get("error_message")
                    else None
                ),
            )
        except Exception as exc:
            log.exception("Error processing task")
            _publish_heartbeat(
                status="error",
                poll_interval=args.poll_interval,
                last_task=last_task,
                error_message=str(exc),
            )
            processed = {"processed": False, "status": "error"}

        if not processed.get("processed") and not _shutdown:
            time.sleep(args.poll_interval)

    _publish_heartbeat(
        status="stopped",
        poll_interval=args.poll_interval,
        last_task=last_task,
    )
    log.info("RDP Task Daemon stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
