"""Regression: MicrostructureCollector.run_forever() 必须按 written/errors/drops
推导 ingest_run status, 不能硬编码 "succeeded".

2026-04-20 code review B-H1 (P0-a 假成功模式在 Bronze 层的残余):
  旧版 run_forever finally 硬编码 finish_ingest_run(..., status="succeeded"),
  DB 下线几小时 thousands of rows 被 buffer drop + flush_errors_total 也涨,
  但 meta.ingest_runs 仍显示 "succeeded" — 运营只能靠 Prometheus counter 判断真相.

新语义:
  total_written == 0 AND flush_errors > 0   → "failed"
  flush_errors > 0 OR rows_dropped > 0      → "retrying" (chk_ir_status 允许)
  else                                       → "succeeded"

本测试用 inspect.getsource + MicrostructureBronzeBuffer 直接行为验证.
不启 ws_client / DB session (需要 asyncio / testcontainers, 放 integration).
"""

from __future__ import annotations

import asyncio
import inspect

from aats.data_platform.collectors.microstructure_ws_collector import (
    MicrostructureBronzeBuffer,
    MicrostructureCollector,
)


# ─────────────────────────────────────────────────────────────────────
# MicrostructureBronzeBuffer: hard-cap drop 计数
# ─────────────────────────────────────────────────────────────────────


def test_buffer_exposes_rows_dropped_total_starts_zero() -> None:
    """新 buffer 的 rows_dropped_total = 0."""
    buf = MicrostructureBronzeBuffer(
        table="test.dummy",
        flush_max_rows=10,
        flush_max_seconds=5.0,
    )
    assert buf.rows_dropped_total == 0


def test_buffer_rows_dropped_increments_on_hardcap_hit() -> None:
    """add() 触发 hard-cap drop 时, rows_dropped_total 累加."""
    # hard cap 是模块常量 _BUFFER_HARD_CAP = 5000, drop_n = 2500
    buf = MicrostructureBronzeBuffer(
        table="test.dummy",
        flush_max_rows=10000,  # 不让 max_rows 阻止
        flush_max_seconds=5.0,
    )
    # 先塞满到 hard cap
    async def _fill():
        for i in range(5000):
            await buf.add(i)
        # 第 5001 个会触发 drop
        await buf.add(5000)

    asyncio.run(_fill())
    # drop 应记录至少 2500 (一半的 hard cap)
    assert buf.rows_dropped_total >= 2500, (
        f"hard-cap hit 后 rows_dropped_total 应 >= 2500, 实际 {buf.rows_dropped_total}"
    )


# ─────────────────────────────────────────────────────────────────────
# run_forever status 推导逻辑 (静态 + 结构化)
# ─────────────────────────────────────────────────────────────────────


def test_run_forever_source_derives_status_not_hardcoded() -> None:
    """契约: MicrostructureCollector.run_forever() 的 finish_ingest_run 调用
    status 必须是变量, 不能是硬编码字符串字面量 "succeeded".
    """
    src = inspect.getsource(MicrostructureCollector.run_forever)

    # 源码中必含 derived_status 变量 (B-H1 fix 引入)
    assert "derived_status" in src, (
        "run_forever 源码必须含 derived_status 变量推导 ingest_run status. "
        "若改回硬编码 status='succeeded', B-H1 修复失效, P0-a 假成功模式复发."
    )
    # 且 finish_ingest_run 调用必须用 status=derived_status
    assert "status=derived_status" in src, (
        "finish_ingest_run(..., status=derived_status) 必须用推导变量, 不能用字面量."
    )


def test_run_forever_source_references_three_status_values() -> None:
    """契约: 三个目标 status 值 (succeeded/retrying/failed) 必须都在源码中出现.

    防止有人只留 succeeded 分支 (即使用 derived_status 变量也可能逻辑上
    永远 = succeeded). 本测试确保三态都被考虑.
    """
    src = inspect.getsource(MicrostructureCollector.run_forever)
    for status in ("succeeded", "retrying", "failed"):
        assert f'"{status}"' in src or f"'{status}'" in src, (
            f"run_forever 源码缺 status={status!r} 分支, 推导可能退化成单一值."
        )


def test_run_forever_source_aggregates_flush_errors_and_drops() -> None:
    """契约: status 推导必须结合 flush_errors_count + rows_dropped_total.

    两者缺一 → 推导逻辑残缺 (例如只看 flush_errors 不看 drops,
    buffer hard-cap 丢了 thousands of rows 但没 flush 报错, 仍会 "succeeded").
    """
    src = inspect.getsource(MicrostructureCollector.run_forever)
    assert "_flush_errors_count" in src, (
        "run_forever 必须读 _flush_errors_count 判断 flush 是否失败过."
    )
    assert "rows_dropped_total" in src, (
        "run_forever 必须读 buffer.rows_dropped_total 判断 hard-cap drop."
    )
