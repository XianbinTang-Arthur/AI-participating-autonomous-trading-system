#!/usr/bin/env python3
"""RDP Task Daemon — 轮询 governance.rdp_task_queue 并执行 workflow.

标准部署把本脚本作为 ``aats-rdp-daemon`` 容器入口，桥接 Gateway UI 的任务
触发和容器内 workflow 执行。直接前台运行仅用于受控开发/诊断，不是第二套
生产部署或后台进程管理入口。

用法:
    # 前台运行（开发调试）
    python scripts/rdp_task_daemon.py

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
    "observation_cycle": 300,  # 5 分钟 — hourly release observation_status 推进
    "reliability_cycle": 300,  # 5 分钟 — hourly current_alerts.json 刷新
    # P1-D Phase 1A: microstructure Silver ETL — 每 15 min 聚合一次,
    # 5 张表各 1 行 UPSERT, 实测 <10s, 留 300s 超时 (设计 §11 p95 < 10s)
    "microstructure_silver_15m": 300,
    # P0-c Option A (2026-04-20): candles rolling 15m — OKX REST 拉 4 symbol × 15m,
    # 纯 collect (跳过 Gold/Gap/Funding), 实测 <30s, 留 300s 超时同 microstructure。
    "candles_rolling_15m": 300,
    # Platform hygiene (2026-04-23): OKX REST history rolling 1h — 拉 OI / mark /
    # long-short 3 端点, task-level timeout 180s, workflow 余量 300s 与同类对齐。
    "okx_rest_history_rolling_1h": 300,
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


def _blocked_workflows() -> frozenset[str]:
    from aats.data_platform.governance.rdp_task_db import ENQUEUE_BLOCKED_WORKFLOWS

    return ENQUEUE_BLOCKED_WORKFLOWS


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
        from aats.data_platform.governance.rdp_runs_db import db_touch_run_heartbeat

        with get_session() as session:
            db_upsert_runtime_status(
                session,
                component=RUNTIME_COMPONENT,
                status=status,
                heartbeat_at=heartbeat_at,
                details=payload,
            )
            if active_task and active_task.get("run_id") and active_task.get("task_id"):
                db_touch_run_heartbeat(
                    session,
                    run_id=str(active_task["run_id"]),
                    task_id=str(active_task["task_id"]),
                    heartbeat_at=heartbeat_at,
                )
    except Exception:
        log.exception("Failed to publish daemon heartbeat")


def _execute_release_cycle_inprocess() -> tuple[int, str, str]:
    """In-process 执行 release_cycle，绕开批次 A 被 stub 的 CLI 入口。

    批次 A 把 ``scripts/rdp_run_release_cycle.py`` 禁用是为了阻止 operator
    手动绕过 apply_token CLI 直接改实盘。daemon 是 trusted 容器进程，不在
    禁用范围之内——原有 subprocess 路径等于让 daemon 自己撞上对外设的 CLI
    闸门，结果是每小时 exit=2 的自引用死循环。此路径直接调用
    ``run_release_cycle`` Python 入口，不再走 scheduler + subprocess。
    """
    import io
    import logging as _logging

    from aats.data_platform.production_workflow.release_cycle import run_release_cycle

    buf = io.StringIO()
    handler = _logging.StreamHandler(buf)
    handler.setLevel(_logging.INFO)
    handler.setFormatter(
        _logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"),
    )
    capture_loggers = [
        _logging.getLogger("aats.data_platform.production_workflow.release_cycle"),
        _logging.getLogger("aats.data_platform.production_workflow.release_registry"),
    ]
    for lg in capture_loggers:
        lg.addHandler(handler)

    try:
        result = run_release_cycle(
            _PROJECT_ROOT,
            actor="rdp_daemon",
            dry_run=False,
            save_results=True,
        )
    except Exception as exc:
        log.exception("In-process release_cycle crashed")
        return (
            1,
            tail_lines(buf.getvalue(), LOG_TAIL_LINES),
            f"in-process release_cycle exception: {exc}"[:500],
        )
    finally:
        for lg in capture_loggers:
            lg.removeHandler(handler)

    failed_count = int(result.get("failed_count") or 0)
    # advisory lock 冲突时 release_cycle 会返回 ok=False + error；这属于"本轮让
    # 位给别的 writer"，不算失败——daemon 应该 exit=0 让下一轮再试。
    lock_error = bool(result.get("error")) and result.get("ok") is False
    exit_code = 0 if (failed_count == 0 and not lock_error) else 1

    summary_lines = [
        "Running workflow: release_cycle (in-process)",
        f"Cycle ID: {result.get('cycle_id')}",
        f"Environment: {result.get('environment')}",
        f"Started: {result.get('started_at')}",
        f"Finished: {result.get('finished_at')}",
        f"Reviewed={result.get('reviewed_count', 0)} "
        f"Eligible={result.get('eligible_count', 0)} "
        f"Selected={result.get('selected_count', 0)}",
        f"Releases created: {result.get('created_release_count', 0)}",
        f"Blocked by gate: {result.get('blocked_count', 0)}",
        f"Failed: {failed_count}",
        f"Skipped: {result.get('skipped_count', 0)}",
    ]
    if result.get("error"):
        summary_lines.append(f"Error: {result['error']}")
    # 保留既有的 success marker 文本，方便既有告警/日志 grep 继续工作。
    summary_lines.append("Release cycle completed")
    summary = "\n".join(summary_lines)

    combined = summary + "\n" + buf.getvalue()
    error_message = ""
    if lock_error:
        error_message = str(result.get("error"))[:500]
    elif failed_count > 0:
        error_message = f"release_cycle failed_count={failed_count}"

    return exit_code, tail_lines(combined, LOG_TAIL_LINES), error_message


def execute_workflow(
    workflow: str,
    *,
    on_progress: Callable[[], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    run_id: str | None = None,
    attempt_no: int | None = None,
) -> tuple[int, str, str]:
    """执行一个 workflow，返回 (exit_code, stdout_tail, error_message)."""
    if workflow in _blocked_workflows():
        message = (
            f"workflow={workflow} is blocked from daemon execution during golden-path freeze"
        )
        log.warning(message)
        return 1, message, message

    # release_cycle 特判：in-process 调用，绕开批次 A 的 CLI stub。其他 workflow
    # 仍走 subprocess，保持 scheduler 通用语义不变。详见 _execute_release_cycle_inprocess。
    if workflow == "release_cycle":
        log.info("Executing in-process: release_cycle")
        if on_progress is not None:
            on_progress()
        return _execute_release_cycle_inprocess()

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
                env={
                    **os.environ,
                    **({"AATS_RDP_RUN_ID": run_id} if run_id else {}),
                    **(
                        {"AATS_RDP_ATTEMPT_NO": str(attempt_no)}
                        if attempt_no is not None else {}
                    ),
                },
            )
            start = time.monotonic()
            while True:
                exit_code = proc.poll()
                if exit_code is not None:
                    output_file.flush()
                    output_file.seek(0)
                    combined = output_file.read()
                    _emit_subprocess_output_to_parent_stdout(workflow, combined)
                    return exit_code, tail_lines(combined, LOG_TAIL_LINES), ""
                if should_cancel is not None and should_cancel():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    output_file.flush()
                    output_file.seek(0)
                    combined = output_file.read()
                    _emit_subprocess_output_to_parent_stdout(workflow, combined)
                    return -4, tail_lines(combined, LOG_TAIL_LINES), "Cancellation requested"
                if time.monotonic() - start >= timeout:
                    proc.kill()
                    proc.wait(timeout=5)
                    output_file.flush()
                    output_file.seek(0)
                    combined = output_file.read()
                    _emit_subprocess_output_to_parent_stdout(workflow, combined)
                    return -1, tail_lines(combined, LOG_TAIL_LINES), f"Timeout after {timeout}s"
                if on_progress is not None:
                    on_progress()
                time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    except Exception as exc:
        return -2, "", str(exc)


def _emit_subprocess_output_to_parent_stdout(workflow: str, combined: str) -> None:
    # rdp_run_scheduled_workflow.py 是 fork 子进程, stdout 被 SpooledTemporaryFile
    # 捕获并仅写入 governance.rdp_task_queue.log_tail. 不把这些行转发到父进程
    # stdout, Promtail/Loki 就永远看不到子进程 log event (例如 silver_microstructure_etl
    # 的 duration 字段), 导致 dashboard p95/avg + sev3-micro-silver-etl-slow 告警
    # 永远是 no-data. 2026-04-22 诊断发现.
    if not combined:
        return
    lines = [line for line in combined.splitlines() if line]
    if not lines:
        return
    log.info("--- workflow %s stdout begin (%d lines) ---", workflow, len(lines))
    for line in lines:
        print(line, flush=True)
    log.info("--- workflow %s stdout end ---", workflow)


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
        db_create_task_if_idle,
        db_update_task_status,
    )

    # claim（在一个事务里 SELECT FOR UPDATE + UPDATE running）
    with get_session() as session:
        task = db_claim_next_task(session)

    if task is None:
        return {"processed": False, "status": "idle"}

    task_id = task["task_id"]
    run_id = task["run_id"]
    attempt_no = int(task["attempt_no"])
    workflow = task["workflow"]
    requested_by = task.get("requested_by")  # R3: auto_retry 防循环判定用
    log.info("=== Processing task %s: workflow=%s ===", task_id, workflow)

    active_task = {
        "task_id": task_id,
        "run_id": run_id,
        "attempt_no": attempt_no,
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

    def _cancel_requested() -> bool:
        from aats.data_platform.governance.rdp_runs_db import (
            db_is_run_cancel_requested,
        )

        with get_session() as cancel_session:
            return db_is_run_cancel_requested(cancel_session, str(run_id))

    if task.get("cancel_requested_at"):
        exit_code, log_tail, error_message = -4, "", "Cancellation requested"
    else:
        exit_code, log_tail, error_message = execute_workflow(
            workflow,
            on_progress=lambda: _publish_heartbeat(
                status="busy",
                poll_interval=poll_interval,
                active_task=active_task,
                last_task=active_task,
            ),
            should_cancel=_cancel_requested,
            run_id=str(run_id),
            attempt_no=attempt_no,
        )

    status = "done" if exit_code == 0 else ("cancelled" if exit_code == -4 else "failed")
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

    # RDP Bug 6 修复: 失败告警
    # 失败时打 structured error log，方便 Loki+Grafana alert rule
    # 抓取（log key=rdp_workflow_failed）。
    # 语义：operator 必须看到每次 workflow failed，不能依赖人工查 DB。
    auto_retry_enqueued: str | None = None
    if status == "failed":
        log.error(
            "rdp_workflow_failed task_id=%s workflow=%s exit_code=%s error=%r",
            task_id, workflow, exit_code, error_message,
            extra={
                "event_name": "rdp_workflow_failed",
                "task_id": task_id,
                "workflow": workflow,
                "exit_code": exit_code,
                "error_message": error_message,
            },
        )

        # R3 Bug 6 retry: 自动产生 15min 延迟 retry task (只 retry 1 次)
        # 防循环: requested_by 带 "auto_retry_of_" 前缀，daemon 对其再失败
        # 不再入队新 retry。scheduler 路径 (requested_by="scheduler") 正常触发。
        # 手动触发 (requested_by 为操作员名) 也 retry 1 次 —— 临时故障应该能自动恢复。
        _RETRY_DELAY_MINUTES = 15
        is_retry_already = str(requested_by or "").startswith("auto_retry_of_")
        if workflow in _blocked_workflows():
            log.warning(
                "rdp_workflow_retry_skipped original=%s workflow=%s reason=golden_path_freeze",
                task_id, workflow,
                extra={
                    "event_name": "rdp_workflow_retry_skipped",
                    "original_task_id": task_id,
                    "workflow": workflow,
                    "reason": "golden_path_freeze",
                },
            )
        elif not is_retry_already:
            from datetime import timedelta as _timedelta

            retry_eligible = _utcnow() + _timedelta(minutes=_RETRY_DELAY_MINUTES)
            try:
                with get_session() as retry_session:
                    retry_task_id, existing = db_create_task_if_idle(
                        retry_session,
                        workflow=workflow,
                        requested_by=f"auto_retry_of_{task_id}",
                        earliest_start_at=retry_eligible,
                        run_id=str(run_id),
                        attempt_no=attempt_no + 1,
                        parent_task_id=str(task_id),
                        trigger_kind="auto_retry",
                    )
                    retry_session.commit()
                if retry_task_id:
                    auto_retry_enqueued = retry_task_id
                    log.warning(
                        "rdp_workflow_retry_enqueued original=%s retry=%s workflow=%s "
                        "earliest_start_at=%s",
                        task_id, retry_task_id, workflow, retry_eligible.isoformat(),
                        extra={
                            "event_name": "rdp_workflow_retry_enqueued",
                            "original_task_id": task_id,
                            "retry_task_id": retry_task_id,
                            "workflow": workflow,
                        },
                    )
                else:
                    # scheduler 已经入队了下轮 task (或前面 auto_retry 还在 pending)
                    log.info(
                        "rdp_workflow_retry_skipped original=%s workflow=%s reason=active_task_present existing=%s",
                        task_id, workflow, (existing or {}).get("task_id"),
                    )
            except Exception:
                log.exception(
                    "rdp_workflow_retry_enqueue_failed original=%s workflow=%s",
                    task_id, workflow,
                )
        else:
            log.warning(
                "rdp_workflow_retry_exhausted original=%s workflow=%s "
                "(retry already failed, no further retry)",
                task_id, workflow,
                extra={
                    "event_name": "rdp_workflow_retry_exhausted",
                    "task_id": task_id,
                    "workflow": workflow,
                },
            )

    log.info("=== Task %s finished: %s (exit=%s) ===", task_id, status, exit_code)
    return {
        "processed": True,
        "task_id": task_id,
        "run_id": run_id,
        "attempt_no": attempt_no,
        "workflow": workflow,
        "status": status,
        "exit_code": exit_code,
        "error_message": error_message or None,
        "auto_retry_enqueued": auto_retry_enqueued,  # R3: retry task_id or None
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

    from aats.data_platform.db import validate_rdp_schema

    validate_rdp_schema()
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
