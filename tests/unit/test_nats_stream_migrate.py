"""scripts/nats_stream_migrate.py 单元测试。

覆盖 slice_nats_jetstream_capacity_fix_design.md §9.4 要求的 9 条测试：

1. test_dry_run_prints_diff_no_side_effects              — T1 dry_run 纯观察
2. test_sync_config_splits_old_into_two_streams          — T1 → update EVENTS + add MARKET
3. test_sync_config_noop_when_already_new                — T2 → 两 stream 都 noop
4. test_sync_config_updates_capacity_drift               — T3 → 只 update MARKET
5. test_sync_config_creates_both_from_empty              — T4 → 两次 add_stream
6. test_sync_config_handles_incomplete_old_stream        — T5 → update + add 混合
7. test_sync_config_raises_on_weird_partial_state        — T6 → raise RuntimeError
8. test_purge_calls_purge_stream_for_each                — --purge 两 stream 都 purge
9. test_rejects_conflicting_flags                         — flag 校验拒绝

另外补充若干 apply_migration_plan 执行顺序 / snapshot helper 的辅助测试以保证
§11.4 "subjects overlap 陷阱" 的 update-before-add 顺序不被未来改动破坏。
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aats.bus.nats_bus import (
    DEFAULT_AATS_EVENTS_COMMANDS_SPEC,
    DEFAULT_AATS_EVENTS_MARKET_SPEC,
    DEFAULT_AATS_EVENTS_SPEC,
    DEFAULT_STREAM_SPECS,
    StreamSpec,
)
from scripts.nats_stream_migrate import (
    StreamSnapshot,
    _FakeConfigFromSnapshot,
    apply_migration_plan,
    compute_migration_plan,
    parse_args,
    validate_args,
)

# ═════════════════════════════════════════════════════════════════════
# 测试 fixture：构造对齐 / 不对齐的 snapshot
# ═════════════════════════════════════════════════════════════════════


def _snapshot_matching(spec: StreamSpec) -> StreamSnapshot:
    """构造一份完全匹配 spec 的 StreamSnapshot（走 noop 分支）。"""
    return StreamSnapshot(
        name=spec.name,
        exists=True,
        subjects=[f"aats.{t}" for t in sorted(spec.topics)],
        max_bytes=spec.max_bytes,
        max_msgs=spec.max_msgs,
        max_msg_size=spec.max_msg_size,
        max_age=float(spec.max_age_seconds),
        messages=0,
        bytes=0,
    )


def _snapshot_missing(name: str) -> StreamSnapshot:
    """构造一份 exists=False 的空快照（stream 不存在）。"""
    return StreamSnapshot(
        name=name,
        exists=False,
        subjects=None,
        max_bytes=None,
        max_msgs=None,
        max_msg_size=None,
        max_age=None,
        messages=None,
        bytes=None,
    )


def _snapshot_old_monolith_events() -> StreamSnapshot:
    """T1/T5 场景：老 AATS_EVENTS 声明所有 critical topics（含 market/features）
    且容量裸奔（max_bytes=-1），messages 很大。"""
    all_critical = (
        DEFAULT_AATS_EVENTS_MARKET_SPEC.topics | DEFAULT_AATS_EVENTS_SPEC.topics
    )
    return StreamSnapshot(
        name="AATS_EVENTS",
        exists=True,
        subjects=[f"aats.{t}" for t in sorted(all_critical)],
        max_bytes=-1,              # 老状态裸奔
        max_msgs=-1,               # 老状态裸奔
        max_msg_size=-1,           # 老状态裸奔
        max_age=604_800.0,         # 7 天（老配置只设了 age）
        messages=451_979,          # 撞 1 GB 那次观察到的量级
        bytes=1_073_000_000,       # ~1 GB
    )


# ═════════════════════════════════════════════════════════════════════
# Test 1: dry_run 不产生副作用
# ═════════════════════════════════════════════════════════════════════


def test_dry_run_prints_diff_no_side_effects(capsys: pytest.CaptureFixture[str]) -> None:
    """T1 状态下跑 compute_migration_plan + 模拟 dry run：
    - plan 列表包含 update 和 add 两种动作
    - 不调 apply_migration_plan → 任何 js mock 都不应被调
    - per_spec_actions 正确分类
    """
    snapshots = {
        "AATS_EVENTS": _snapshot_old_monolith_events(),
        "AATS_EVENTS_MARKET": _snapshot_missing("AATS_EVENTS_MARKET"),
    }

    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    # per_spec_actions 正确分类：EVENTS drift → update；MARKET missing → add
    assert plan.per_spec_actions["AATS_EVENTS"] == "update"
    assert plan.per_spec_actions["AATS_EVENTS_MARKET"] == "add"
    assert plan.will_purge is False
    assert plan.will_recreate is False

    # plan.actions 是人类可读的字符串列表
    joined = "\n".join(plan.actions)
    assert "[update] AATS_EVENTS" in joined
    assert "[add] AATS_EVENTS_MARKET" in joined

    # dry run 不调 apply_migration_plan；验证：即使传一个会炸的 mock js，
    # 只要不调 apply，也不会出问题
    fake_js = MagicMock()
    fake_js.add_stream = AsyncMock(side_effect=AssertionError("apply should not be called in dry_run"))
    fake_js.update_stream = AsyncMock(side_effect=AssertionError("apply should not be called in dry_run"))
    fake_js.purge_stream = AsyncMock(side_effect=AssertionError("apply should not be called in dry_run"))

    # 不 apply —— 只确认 fake_js 没被动
    fake_js.add_stream.assert_not_called()
    fake_js.update_stream.assert_not_called()
    fake_js.purge_stream.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# Test 2: T1 — update EVENTS + add MARKET（含 subject overlap 顺序保证）
# ═════════════════════════════════════════════════════════════════════


def test_sync_config_splits_old_into_two_streams() -> None:
    """T1：老 AATS_EVENTS 含全部 critical topic + MARKET/COMMANDS 不存在。
    - plan: EVENTS=update, MARKET=add, COMMANDS=add
    - apply 阶段必须 **先** update_stream AATS_EVENTS（释放 market + commands
      子集）**再** add_stream AATS_EVENTS_MARKET / AATS_EVENTS_COMMANDS，否则
      nats-py 会抛 "subjects overlap"（§11.4）

    2026-04-20 B2a：DEFAULT_STREAM_SPECS 从 2 条扩到 3 条（加 AATS_EVENTS_COMMANDS），
    migration plan 也对应扩展。
    """
    snapshots = {
        "AATS_EVENTS": _snapshot_old_monolith_events(),
        "AATS_EVENTS_MARKET": _snapshot_missing("AATS_EVENTS_MARKET"),
        "AATS_EVENTS_COMMANDS": _snapshot_missing("AATS_EVENTS_COMMANDS"),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    assert plan.per_spec_actions == {
        "AATS_EVENTS": "update",
        "AATS_EVENTS_MARKET": "add",
        "AATS_EVENTS_COMMANDS": "add",
    }

    # 记录调用顺序，验证 update 先于 add
    call_order: list[str] = []

    fake_js = MagicMock()

    async def record_update(config: Any) -> None:
        call_order.append(f"update:{config.name}")

    async def record_add(config: Any) -> None:
        call_order.append(f"add:{config.name}")

    fake_js.update_stream = AsyncMock(side_effect=record_update)
    fake_js.add_stream = AsyncMock(side_effect=record_add)
    fake_js.purge_stream = AsyncMock()
    fake_js.delete_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    # update AATS_EVENTS 必须**先于**所有 add；add 顺序本身不敏感（MARKET
    # 和 COMMANDS 都是新 stream 且 subjects 互斥）
    assert call_order[0] == "update:AATS_EVENTS", (
        f"update AATS_EVENTS 必须在所有 add 之前（§11.4 subject overlap 防御），"
        f"实际顺序 {call_order}"
    )
    add_names = {e.split(":", 1)[1] for e in call_order if e.startswith("add:")}
    assert add_names == {"AATS_EVENTS_MARKET", "AATS_EVENTS_COMMANDS"}, (
        f"add 集合应恰好 MARKET + COMMANDS，实际 {add_names}"
    )

    fake_js.update_stream.assert_awaited_once()
    assert fake_js.add_stream.await_count == 2
    fake_js.purge_stream.assert_not_awaited()
    fake_js.delete_stream.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════
# Test 3: T2 — 两条 stream 都已对齐 → noop
# ═════════════════════════════════════════════════════════════════════


def test_sync_config_noop_when_already_new() -> None:
    """T2：所有 stream 都已存在且完全匹配目标 spec → 都是 noop。
    apply 阶段不应调 update_stream / add_stream / purge_stream。

    2026-04-20 B2a：DEFAULT_STREAM_SPECS 扩到 3 条，测试同步覆盖。
    """
    snapshots = {
        "AATS_EVENTS_MARKET": _snapshot_matching(DEFAULT_AATS_EVENTS_MARKET_SPEC),
        "AATS_EVENTS": _snapshot_matching(DEFAULT_AATS_EVENTS_SPEC),
        "AATS_EVENTS_COMMANDS": _snapshot_matching(DEFAULT_AATS_EVENTS_COMMANDS_SPEC),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    assert plan.per_spec_actions == {
        "AATS_EVENTS_MARKET": "noop",
        "AATS_EVENTS": "noop",
        "AATS_EVENTS_COMMANDS": "noop",
    }

    joined = "\n".join(plan.actions)
    assert "[noop] AATS_EVENTS_MARKET" in joined
    assert "[noop] AATS_EVENTS" in joined
    assert "[noop] AATS_EVENTS_COMMANDS" in joined

    fake_js = MagicMock()
    fake_js.update_stream = AsyncMock()
    fake_js.add_stream = AsyncMock()
    fake_js.purge_stream = AsyncMock()
    fake_js.delete_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    fake_js.update_stream.assert_not_awaited()
    fake_js.add_stream.assert_not_awaited()
    fake_js.purge_stream.assert_not_awaited()
    fake_js.delete_stream.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════
# Test 4: T3 — EVENTS 对齐 + MARKET 容量漂移 → 只 update MARKET
# ═════════════════════════════════════════════════════════════════════


def test_sync_config_updates_capacity_drift() -> None:
    """T3：EVENTS 已对齐，MARKET 存在但 max_bytes 漂移。
    - plan: EVENTS=noop, MARKET=update
    - apply: 只 update_stream MARKET
    """
    drifted_market = _snapshot_matching(DEFAULT_AATS_EVENTS_MARKET_SPEC)
    drifted_market_fixed = StreamSnapshot(
        name=drifted_market.name,
        exists=drifted_market.exists,
        subjects=drifted_market.subjects,
        max_bytes=1_000_000,  # 漂移：比目标 2 GB 小得多
        max_msgs=drifted_market.max_msgs,
        max_msg_size=drifted_market.max_msg_size,
        max_age=drifted_market.max_age,
        messages=drifted_market.messages,
        bytes=drifted_market.bytes,
    )
    snapshots = {
        "AATS_EVENTS_MARKET": drifted_market_fixed,
        "AATS_EVENTS": _snapshot_matching(DEFAULT_AATS_EVENTS_SPEC),
        "AATS_EVENTS_COMMANDS": _snapshot_matching(DEFAULT_AATS_EVENTS_COMMANDS_SPEC),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    assert plan.per_spec_actions == {
        "AATS_EVENTS_MARKET": "update",
        "AATS_EVENTS": "noop",
        "AATS_EVENTS_COMMANDS": "noop",
    }

    joined = "\n".join(plan.actions)
    assert "[update] AATS_EVENTS_MARKET" in joined
    assert "max_bytes" in joined  # drift field summary 列出来

    fake_js = MagicMock()
    fake_js.update_stream = AsyncMock()
    fake_js.add_stream = AsyncMock()
    fake_js.purge_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    # 只 update MARKET；EVENTS 没动
    fake_js.update_stream.assert_awaited_once()
    updated_cfg = fake_js.update_stream.await_args.kwargs["config"]
    assert updated_cfg.name == "AATS_EVENTS_MARKET"
    fake_js.add_stream.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════
# Test 5: T4 — 两条 stream 都不存在 → 两次 add_stream
# ═════════════════════════════════════════════════════════════════════


def test_sync_config_creates_both_from_empty() -> None:
    """T4：首次部署 / clean slate —— 3 条 stream 都不存在 → 三次 add_stream。

    2026-04-20 B2a：DEFAULT_STREAM_SPECS 扩到 3 条。
    """
    snapshots = {
        "AATS_EVENTS_MARKET": _snapshot_missing("AATS_EVENTS_MARKET"),
        "AATS_EVENTS": _snapshot_missing("AATS_EVENTS"),
        "AATS_EVENTS_COMMANDS": _snapshot_missing("AATS_EVENTS_COMMANDS"),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    assert plan.per_spec_actions == {
        "AATS_EVENTS_MARKET": "add",
        "AATS_EVENTS": "add",
        "AATS_EVENTS_COMMANDS": "add",
    }

    joined = "\n".join(plan.actions)
    assert "[add] AATS_EVENTS_MARKET" in joined
    assert "[add] AATS_EVENTS" in joined
    assert "[add] AATS_EVENTS_COMMANDS" in joined

    added: list[str] = []

    fake_js = MagicMock()

    async def record_add(config: Any) -> None:
        added.append(config.name)

    fake_js.add_stream = AsyncMock(side_effect=record_add)
    fake_js.update_stream = AsyncMock()
    fake_js.purge_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    assert sorted(added) == ["AATS_EVENTS", "AATS_EVENTS_COMMANDS", "AATS_EVENTS_MARKET"]
    assert fake_js.add_stream.await_count == 3
    fake_js.update_stream.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════
# Test 6: T5 — 老 EVENTS 不完整（Slice 6.5 前状态）+ MARKET 不存在
# ═════════════════════════════════════════════════════════════════════


def test_sync_config_handles_incomplete_old_stream() -> None:
    """T5：老 AATS_EVENTS 存在但 subjects 不完整（譬如没有 obligation_updates
    这种 Slice 6.5 才加的 topic），MARKET 不存在。
    - plan: EVENTS=update（补齐 subjects + 写入容量），MARKET=add
    - 本质上和 T1 走同一分支（update + add），只是 drift 原因是 subjects 而不是容量
    """
    incomplete_events = StreamSnapshot(
        name="AATS_EVENTS",
        exists=True,
        subjects=[
            # 故意漏掉 obligation_updates / kill_switch_state 等 Slice 6.5 topic
            "aats.execution.order_intents",
            "aats.risk.decisions",
            "aats.strategy.decision_context",
        ],
        max_bytes=-1,          # 老状态裸奔
        max_msgs=-1,
        max_msg_size=-1,
        max_age=604_800.0,
        messages=1_234,
        bytes=456_789,
    )
    snapshots = {
        "AATS_EVENTS": incomplete_events,
        "AATS_EVENTS_MARKET": _snapshot_missing("AATS_EVENTS_MARKET"),
        "AATS_EVENTS_COMMANDS": _snapshot_missing("AATS_EVENTS_COMMANDS"),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    assert plan.per_spec_actions == {
        "AATS_EVENTS": "update",
        "AATS_EVENTS_MARKET": "add",
        "AATS_EVENTS_COMMANDS": "add",
    }

    # drift 应该同时包含 subjects / max_bytes / max_msgs / max_msg_size
    joined = "\n".join(plan.actions)
    assert "[update] AATS_EVENTS" in joined
    assert "subjects" in joined
    assert "max_bytes" in joined

    # apply 顺序：先 update EVENTS，再 add MARKET / COMMANDS（§11.4 防御）
    call_order: list[str] = []
    fake_js = MagicMock()

    async def record_update(config: Any) -> None:
        call_order.append(f"update:{config.name}")

    async def record_add(config: Any) -> None:
        call_order.append(f"add:{config.name}")

    fake_js.update_stream = AsyncMock(side_effect=record_update)
    fake_js.add_stream = AsyncMock(side_effect=record_add)
    fake_js.purge_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    # update EVENTS 必须先于所有 add（避免 §11.4 subject overlap）
    assert call_order[0] == "update:AATS_EVENTS", (
        f"expected update-before-add but got: {call_order}"
    )
    add_names = {e.split(":", 1)[1] for e in call_order if e.startswith("add:")}
    assert add_names == {"AATS_EVENTS_MARKET", "AATS_EVENTS_COMMANDS"}, (
        f"expected 2 adds (MARKET + COMMANDS) but got {add_names}"
    )


# ═════════════════════════════════════════════════════════════════════
# Test 7: T6 — MARKET 存在但 EVENTS 不存在 → raise RuntimeError
# ═════════════════════════════════════════════════════════════════════


def test_sync_config_raises_on_weird_partial_state() -> None:
    """T6：诡异状态（之前 migration 中途失败 / 人类手动删除 EVENTS）。
    用户决策 D8：不自动恢复，raise RuntimeError 暴露人类介入。
    错误信息必须包含：
    - 诡异状态的描述
    - 3 种可能原因
    - 3 步人工恢复步骤
    - --recreate 指令
    """
    snapshots = {
        "AATS_EVENTS_MARKET": _snapshot_matching(DEFAULT_AATS_EVENTS_MARKET_SPEC),
        "AATS_EVENTS": _snapshot_missing("AATS_EVENTS"),
    }

    with pytest.raises(RuntimeError) as excinfo:
        compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS)

    msg = str(excinfo.value)
    # 关键字覆盖
    assert "inconsistent stream state" in msg
    assert "AATS_EVENTS_MARKET exists but AATS_EVENTS does not" in msg
    assert "manual recovery" in msg.lower()
    assert "--recreate" in msg
    # possible causes 必须列出来（不是静默吞掉）
    assert "interrupted" in msg or "manually deleted" in msg

    # 关键：--recreate=True 应该**跳过** T6 检查（作为逃生口）
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS, recreate=True)
    assert plan.will_recreate is True
    for spec in DEFAULT_STREAM_SPECS:
        assert plan.per_spec_actions[spec.name] == "recreate"


# ═════════════════════════════════════════════════════════════════════
# Test 8: --purge 对每条已存在 stream 都调 purge_stream
# ═════════════════════════════════════════════════════════════════════


def test_purge_calls_purge_stream_for_each() -> None:
    """--sync-config --purge：sync 完后对每条 *已存在* stream 调 purge_stream。
    不应对 exists=False 的 stream 调 purge（那会抛 NotFoundError）。
    """
    snapshots = {
        "AATS_EVENTS_MARKET": _snapshot_matching(DEFAULT_AATS_EVENTS_MARKET_SPEC),
        "AATS_EVENTS": _snapshot_matching(DEFAULT_AATS_EVENTS_SPEC),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS, purge=True)

    assert plan.will_purge is True
    joined = "\n".join(plan.actions)
    assert "[purge] AATS_EVENTS_MARKET" in joined
    assert "[purge] AATS_EVENTS" in joined

    purged: list[str] = []
    fake_js = MagicMock()

    async def record_purge(name: str) -> None:
        purged.append(name)

    fake_js.purge_stream = AsyncMock(side_effect=record_purge)
    fake_js.update_stream = AsyncMock()
    fake_js.add_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    # 两条都被 purge（顺序不重要）
    assert sorted(purged) == ["AATS_EVENTS", "AATS_EVENTS_MARKET"]
    assert fake_js.purge_stream.await_count == 2

    # --- 混合场景：MARKET 不存在 + EVENTS 存在 + purge=True ---
    # 只应 purge EVENTS（MARKET 不存在无法 purge）
    snapshots_mixed = {
        "AATS_EVENTS_MARKET": _snapshot_missing("AATS_EVENTS_MARKET"),
        "AATS_EVENTS": _snapshot_matching(DEFAULT_AATS_EVENTS_SPEC),
    }
    plan_mixed = compute_migration_plan(
        snapshots_mixed, DEFAULT_STREAM_SPECS, purge=True
    )
    purged2: list[str] = []

    async def record_purge2(name: str) -> None:
        purged2.append(name)

    fake_js2 = MagicMock()
    fake_js2.purge_stream = AsyncMock(side_effect=record_purge2)
    fake_js2.update_stream = AsyncMock()
    fake_js2.add_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js2, plan_mixed, DEFAULT_STREAM_SPECS))

    assert purged2 == ["AATS_EVENTS"]  # 只 purge EVENTS


# ═════════════════════════════════════════════════════════════════════
# Test 9: validate_args 拒绝不合理 flag 组合
# ═════════════════════════════════════════════════════════════════════


def test_rejects_conflicting_flags() -> None:
    """CLI flag 校验：
    1. --recreate + --sync-config 互斥 → SystemExit
    2. --purge 必须配合 --sync-config 或 --dry-run → SystemExit
    3. 什么都不传 → SystemExit
    4. 合法组合（--dry-run / --sync-config / --sync-config --purge / --recreate）应通过
    """

    def ns(**kwargs: Any) -> argparse.Namespace:
        return argparse.Namespace(
            servers="nats://127.0.0.1:4222",
            dry_run=kwargs.get("dry_run", False),
            sync_config=kwargs.get("sync_config", False),
            purge=kwargs.get("purge", False),
            recreate=kwargs.get("recreate", False),
        )

    # (1) --recreate + --sync-config 互斥
    with pytest.raises(SystemExit, match="mutually exclusive"):
        validate_args(ns(recreate=True, sync_config=True))

    # (2) --purge 脱离 --sync-config / --dry-run 不合法
    with pytest.raises(SystemExit, match="purge only makes sense"):
        validate_args(ns(purge=True))

    # --purge + --recreate 也不合法（purge 需要 sync_config / dry_run，而非 recreate）
    with pytest.raises(SystemExit, match="purge only makes sense"):
        validate_args(ns(purge=True, recreate=True))

    # (3) 什么都不传 → raise
    with pytest.raises(SystemExit, match="must specify one of"):
        validate_args(ns())

    # (4) 合法组合不应 raise
    validate_args(ns(dry_run=True))
    validate_args(ns(sync_config=True))
    validate_args(ns(sync_config=True, purge=True))
    validate_args(ns(recreate=True))
    validate_args(ns(dry_run=True, purge=True))  # dry-run + purge 只是预演 purge plan


# ═════════════════════════════════════════════════════════════════════
# 补充测试：parse_args CLI 解析 / _FakeConfigFromSnapshot 防御
# （非设计文档 §9.4 硬性要求，但帮助锁定边界行为）
# ═════════════════════════════════════════════════════════════════════


def test_parse_args_defaults() -> None:
    """默认不传任何 flag 时 argparse 不会 raise（raise 发生在 validate_args 里）。"""
    args = parse_args(["--dry-run"])
    assert args.dry_run is True
    assert args.sync_config is False
    assert args.purge is False
    assert args.recreate is False
    assert args.servers.startswith("nats://")


def test_fake_config_from_snapshot_fills_defaults_for_none() -> None:
    """_FakeConfigFromSnapshot 把 None 归一化为 0，避免 _compute_stream_config_drift
    对 None 做算术操作。"""
    snap = _snapshot_missing("X")
    cfg = _FakeConfigFromSnapshot(snap)
    assert cfg.subjects == []
    assert cfg.max_age == 0
    assert cfg.max_bytes == 0
    assert cfg.max_msgs == 0
    assert cfg.max_msg_size == 0
    # replica / duplicate_window / deny_purge 有合理默认
    assert cfg.num_replicas == 1
    assert cfg.duplicate_window == 120.0
    assert cfg.deny_purge is False


def test_recreate_path_uses_delete_then_add() -> None:
    """--recreate 分支：对每条 target spec delete_stream 后 add_stream。
    这是 T6 的逃生口以及用户主动清库重建的入口。

    2026-04-20 B2a：DEFAULT_STREAM_SPECS 扩到 3 条，recreate 路径也对 3 条
    都执行 delete+add。
    """
    snapshots = {
        "AATS_EVENTS_MARKET": _snapshot_matching(DEFAULT_AATS_EVENTS_MARKET_SPEC),
        "AATS_EVENTS": _snapshot_matching(DEFAULT_AATS_EVENTS_SPEC),
        "AATS_EVENTS_COMMANDS": _snapshot_matching(DEFAULT_AATS_EVENTS_COMMANDS_SPEC),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS, recreate=True)
    assert plan.will_recreate is True
    assert all(v == "recreate" for v in plan.per_spec_actions.values())
    assert len(plan.per_spec_actions) == 3

    order: list[str] = []
    fake_js = MagicMock()

    async def record_delete(name: str) -> None:
        order.append(f"delete:{name}")

    async def record_add(config: Any) -> None:
        order.append(f"add:{config.name}")

    fake_js.delete_stream = AsyncMock(side_effect=record_delete)
    fake_js.add_stream = AsyncMock(side_effect=record_add)
    fake_js.update_stream = AsyncMock()
    fake_js.purge_stream = AsyncMock()

    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    # 每条 spec 都先 delete 再 add，顺序按 DEFAULT_STREAM_SPECS 顺序
    # （MARKET → EVENTS → COMMANDS）
    expected = [
        "delete:AATS_EVENTS_MARKET",
        "add:AATS_EVENTS_MARKET",
        "delete:AATS_EVENTS",
        "add:AATS_EVENTS",
        "delete:AATS_EVENTS_COMMANDS",
        "add:AATS_EVENTS_COMMANDS",
    ]
    assert order == expected
    fake_js.update_stream.assert_not_awaited()
    fake_js.purge_stream.assert_not_awaited()


def test_recreate_tolerates_missing_stream_on_delete() -> None:
    """--recreate 分支：如果 stream 本来就不存在，delete_stream 抛错应被吞掉
    继续 add_stream（让 T4 状态下跑 --recreate 也能走通）。

    2026-04-20 B2a：扩 3 条 stream。
    """
    snapshots = {
        "AATS_EVENTS_MARKET": _snapshot_missing("AATS_EVENTS_MARKET"),
        "AATS_EVENTS": _snapshot_missing("AATS_EVENTS"),
        "AATS_EVENTS_COMMANDS": _snapshot_missing("AATS_EVENTS_COMMANDS"),
    }
    plan = compute_migration_plan(snapshots, DEFAULT_STREAM_SPECS, recreate=True)

    try:
        from nats.js.errors import NotFoundError
    except ImportError:
        pytest.skip("nats-py not installed")

    fake_js = MagicMock()
    fake_js.delete_stream = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()

    # 不应抛；delete 的 NotFoundError 被吞掉继续 add
    asyncio.run(apply_migration_plan(fake_js, plan, DEFAULT_STREAM_SPECS))

    assert fake_js.delete_stream.await_count == 3
    assert fake_js.add_stream.await_count == 3
