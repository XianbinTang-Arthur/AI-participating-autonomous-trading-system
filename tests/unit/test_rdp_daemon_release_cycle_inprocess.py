"""Daemon release_cycle execution contracts.

Golden-path freeze 下, release_cycle 既不能新入队,也不能由 daemon 执行
pre-patch 残留任务。底层 in-process helper 仍保留并由直接单测覆盖,方便未来
明确解冻时复用,但 execute_workflow 当前必须先命中 freeze guard。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch


def _load_daemon_module():
    """按路径加载 scripts/rdp_task_daemon.py 模块。

    daemon 是 script(带 ``__main__`` 守卫),但其顶层函数可被作为模块 import。
    """
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "scripts"))
    try:
        return importlib.import_module("rdp_task_daemon")
    finally:
        sys.path.remove(str(root / "scripts"))


def test_release_cycle_execution_is_blocked_under_golden_path_freeze() -> None:
    """核心契约:execute_workflow('release_cycle') 不能触发 release/apply。"""
    daemon = _load_daemon_module()

    captured = {}

    def fake_inprocess() -> tuple[int, str, str]:
        captured["called"] = True
        return 0, "Release cycle completed\nmock tail", ""

    with (
        patch.object(daemon, "_execute_release_cycle_inprocess", fake_inprocess),
        patch.object(daemon.subprocess, "Popen") as fake_popen,
    ):
        exit_code, tail, err = daemon.execute_workflow("release_cycle")

    assert captured.get("called") is None, "freeze 下不得调用 release_cycle in-process"
    assert fake_popen.call_count == 0, "freeze 下也不得回退到 subprocess"
    assert exit_code == 1
    assert "golden-path freeze" in tail
    assert "golden-path freeze" in err


def test_other_workflows_still_use_subprocess() -> None:
    """保护回归:research_cycle / governance_cycle / data_maintenance 等仍走 subprocess。

    in-process 是 release_cycle 的特判,如果其他 workflow 也被误 route,会导致
    一堆目前稳定跑着的 workflow 丢掉 timeout 保护和 subprocess 隔离。
    """
    daemon = _load_daemon_module()

    class _FakeProc:
        def poll(self) -> int:
            # 一上来就返回 0,让 execute_workflow 的 polling 循环第一轮就 break,
            # 不触发 time.sleep——测试就不用 patch time。
            return 0

        def kill(self) -> None: ...
        def wait(self, timeout: int | None = None) -> int: ...

    def fake_popen(*args, **kwargs):
        # 写一行假输出到 capture file,让 execute_workflow 有东西 tail
        output_file = kwargs.get("stdout")
        if output_file is not None:
            output_file.write("research ok\n")
        return _FakeProc()

    inprocess_calls = []

    def fake_inprocess() -> tuple[int, str, str]:
        inprocess_calls.append(True)
        return 0, "", ""

    with (
        patch.object(daemon.subprocess, "Popen", side_effect=fake_popen) as popen_spy,
        patch.object(daemon, "_execute_release_cycle_inprocess", fake_inprocess),
    ):
        for wf in ("research_cycle", "governance_cycle", "data_maintenance"):
            daemon.execute_workflow(wf)

    assert popen_spy.call_count == 3, "其他 workflow 必须继续走 subprocess"
    assert inprocess_calls == [], (
        "只有 release_cycle 能走 in-process;其他 workflow 误 route 会 strip 掉 subprocess 的 timeout 保护"
    )


def test_inprocess_release_cycle_maps_failed_count_to_exit_one() -> None:
    """``run_release_cycle`` 报告 failed_count>0 时,daemon 必须返回 exit_code=1。"""
    daemon = _load_daemon_module()

    fake_result = {
        "cycle_id": "relcy_test",
        "environment": "test",
        "started_at": "2026-04-18T12:00:00+00:00",
        "finished_at": "2026-04-18T12:00:05+00:00",
        "reviewed_count": 3,
        "eligible_count": 2,
        "selected_count": 2,
        "created_release_count": 1,
        "blocked_count": 0,
        "failed_count": 1,
        "skipped_count": 0,
        "results": [],
    }

    with patch(
        "aats.data_platform.production_workflow.release_cycle.run_release_cycle",
        return_value=fake_result,
    ):
        exit_code, tail, err = daemon._execute_release_cycle_inprocess()

    assert exit_code == 1
    assert "failed_count=1" in err
    assert "Release cycle completed" in tail  # success marker 字段仍然 emit,便于现有 grep


def test_inprocess_release_cycle_happy_path_exits_zero() -> None:
    """没有失败时 exit_code=0 + error_message 为空。"""
    daemon = _load_daemon_module()

    fake_result = {
        "cycle_id": "relcy_test_ok",
        "environment": "live",
        "started_at": "2026-04-18T13:00:00+00:00",
        "finished_at": "2026-04-18T13:00:05+00:00",
        "reviewed_count": 5,
        "eligible_count": 2,
        "selected_count": 2,
        "created_release_count": 2,
        "blocked_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
    }

    with patch(
        "aats.data_platform.production_workflow.release_cycle.run_release_cycle",
        return_value=fake_result,
    ):
        exit_code, tail, err = daemon._execute_release_cycle_inprocess()

    assert exit_code == 0
    assert err == ""
    assert "Releases created: 2" in tail


def test_inprocess_release_cycle_treats_advisory_lock_conflict_as_noop_success() -> None:
    """advisory lock 冲突(另一个 writer 正在跑)不是错误——让位给别人,下一轮再试。

    如果把 lock 冲突当失败,daemon 每小时会把 rdp_task_queue 标 failed,noise
    很大;实际上这只是"本轮 skip"。run_release_cycle 通过 ok=False + error
    短语表达这个情况。
    """
    daemon = _load_daemon_module()

    fake_result = {
        "ok": False,
        "cycle_id": "relcy_locked",
        "environment": "live",
        "started_at": "2026-04-18T14:00:00+00:00",
        "finished_at": "2026-04-18T14:00:00+00:00",
        "reviewed_count": 0,
        "eligible_count": 0,
        "selected_count": 0,
        "created_release_count": 0,
        "blocked_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
        "error": "另一个 release_cycle 正在运行（advisory lock 被持有）",
    }

    with patch(
        "aats.data_platform.production_workflow.release_cycle.run_release_cycle",
        return_value=fake_result,
    ):
        exit_code, tail, err = daemon._execute_release_cycle_inprocess()

    # lock 冲突:虽然 ok=False,但不算 failure——daemon 应 exit=1 才能让队列
    # 知道这次没干活(否则被误认成 done 不再重试);保留 error_message 方便审计。
    assert exit_code == 1
    assert "advisory lock" in err
    assert "advisory lock" in tail


def test_inprocess_release_cycle_catches_exception_and_returns_exit_one() -> None:
    """run_release_cycle 抛异常 → exit=1 + 异常 message 收敛到 500 字符内。"""
    daemon = _load_daemon_module()

    class _Boom(RuntimeError):
        pass

    with patch(
        "aats.data_platform.production_workflow.release_cycle.run_release_cycle",
        side_effect=_Boom("DB connection refused"),
    ):
        exit_code, tail, err = daemon._execute_release_cycle_inprocess()

    assert exit_code == 1
    assert "DB connection refused" in err
    assert len(err) <= 500
