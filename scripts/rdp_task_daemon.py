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
import logging
import signal
import subprocess
import sys
import time
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
WORKFLOW_TIMEOUTS = {
    "data_maintenance": 900,   # 15 分钟
    "research_cycle": 3600,    # 60 分钟
}
DEFAULT_TIMEOUT = 1800  # 30 分钟

# 保留最后 N 行日志
LOG_TAIL_LINES = 50

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
    return p.parse_args()


def tail_lines(text: str, n: int) -> str:
    """取最后 n 行."""
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def execute_workflow(workflow: str) -> tuple[int, str, str]:
    """执行一个 workflow，返回 (exit_code, stdout_tail, error_message)."""
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "scripts" / "rdp_run_scheduled_workflow.py"),
        "--workflow", workflow,
    ]
    timeout = WORKFLOW_TIMEOUTS.get(workflow, DEFAULT_TIMEOUT)

    log.info("Executing: %s (timeout=%ds)", " ".join(cmd), timeout)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(_PROJECT_ROOT),
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, tail_lines(combined, LOG_TAIL_LINES), ""
    except subprocess.TimeoutExpired as exc:
        combined = ((exc.stdout or "") + (exc.stderr or "")) if exc.stdout or exc.stderr else ""
        return -1, tail_lines(combined, LOG_TAIL_LINES), f"Timeout after {timeout}s"
    except Exception as exc:
        return -2, "", str(exc)


def process_one_task() -> bool:
    """尝试领取并执行一个任务。返回是否处理了任务."""
    from aats.data_platform.db import get_session
    from aats.data_platform.governance.rdp_task_db import (
        db_claim_next_task,
        db_update_task_status,
    )

    # claim（在一个事务里 SELECT FOR UPDATE + UPDATE running）
    with get_session() as session:
        task = db_claim_next_task(session)

    if task is None:
        return False

    task_id = task["task_id"]
    workflow = task["workflow"]
    log.info("=== Processing task %s: workflow=%s ===", task_id, workflow)

    exit_code, log_tail, error_message = execute_workflow(workflow)

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
    return True


def main() -> int:
    args = parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("RDP Task Daemon started (poll_interval=%ds, once=%s)",
             args.poll_interval, args.once)

    if args.once:
        processed = process_one_task()
        if not processed:
            log.info("No pending tasks.")
        return 0

    heartbeat_path = Path("/tmp/rdp_daemon_alive")

    while not _shutdown:
        try:
            processed = process_one_task()
        except Exception:
            log.exception("Error processing task")
            processed = False

        # touch heartbeat 供 Docker healthcheck 使用
        try:
            heartbeat_path.touch()
        except OSError:
            pass

        if not processed and not _shutdown:
            time.sleep(args.poll_interval)

    log.info("RDP Task Daemon stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
