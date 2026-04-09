"""NATS AATS events stream 容量策略 + 分层归属 migration 脚本。

用途
====

1. 把老的单 ``AATS_EVENTS`` stream（含所有 critical topic）迁移到 slice
   nats-capacity 的分层架构（``AATS_EVENTS`` + ``AATS_EVENTS_MARKET`` 两个 stream）
2. 对每个 stream 同步容量策略到最新 StreamSpec（通过 drift 比较）
3. 可选清洗：purge 遗留的累积观察运行噪音数据
4. 支持 --dry-run 只打印计划不执行

幂等性
======

- 跑 N 次效果等价于跑 1 次
- 已经是新配置则每个 stream 各自 noop
- stream 不存在则 add_stream 创建
- 对每个 stream 独立 purge

用法
====

::

    # 只看当前 stream 状态 + 差异报告（每 stream 各一段），不改 state
    python scripts/nats_stream_migrate.py --dry-run

    # 同步容量策略 + 拆分 AATS_EVENTS → (AATS_EVENTS, AATS_EVENTS_MARKET)
    python scripts/nats_stream_migrate.py --sync-config

    # 同步 + purge 历史数据（dev 推荐，用户确认分库分表测试不影响）
    python scripts/nats_stream_migrate.py --sync-config --purge

    # 完全重建所有 stream（delete + recreate；最激进；会丢所有数据）
    python scripts/nats_stream_migrate.py --recreate

迁移矩阵（6 种出发状态）
========================

见 docs/task/slice_nats_jetstream_capacity_fix_design.md §9.2：

- T1: 老 AATS_EVENTS 存在（含全部 critical）+ MARKET 不存在 → update + add
- T2: 两个 stream 都存在且完全对齐 → noop
- T3: EVENTS 新状态 + MARKET 容量漂移 → 只 update MARKET
- T4: 两个 stream 都不存在 → add 两个
- T5: EVENTS 不完整（譬如 Slice 6.5 前状态）+ MARKET 不存在 → update + add
- T6: **诡异状态** MARKET 存在但 EVENTS 不存在 → **raise RuntimeError**

T6 raise 语义：用户决策 D8 "要"——不自动恢复，暴露人类介入。

设计文档：docs/task/slice_nats_jetstream_capacity_fix_design.md §9
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

# 允许从 scripts/ 目录跑（WORKDIR /app）
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aats.bus.nats_bus import (  # noqa: E402
    DEFAULT_STREAM_SPECS,
    StreamSpec,
    _compute_stream_config_drift,
    build_nats_streams_from_env,
)


# ═════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class StreamSnapshot:
    """单条 stream 的当前状态快照（从 stream_info 拿到的 minimal view）。"""

    name: str
    exists: bool
    subjects: list[str] | None
    max_bytes: int | None
    max_msgs: int | None
    max_msg_size: int | None
    max_age: float | None
    messages: int | None
    bytes: int | None


@dataclass(slots=True)
class MigrationPlan:
    """一次 migration 的整体计划（所有 stream 合并）。"""

    snapshots: dict[str, StreamSnapshot]  # name → snapshot
    actions: list[str]                    # 人类可读的动作列表
    # 每个 spec 对应的具体动作：("add" | "update" | "noop", StreamSpec)
    per_spec_actions: dict[str, str]
    will_purge: bool
    will_recreate: bool


# ═════════════════════════════════════════════════════════════════════
# NATS 连接 helper（纯运行时依赖注入方便测试）
# ═════════════════════════════════════════════════════════════════════


async def _open_jetstream(servers: list[str]) -> tuple[Any, Any]:
    """连接 NATS 并返回 (client, jetstream) 以便上游 close。"""
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "nats-py is required for nats_stream_migrate.py. "
            "Install with: pip install nats-py"
        ) from exc

    client = await nats.connect(servers=servers, connect_timeout=5)
    js = client.jetstream()
    return client, js


async def _snapshot_stream(js: Any, name: str) -> StreamSnapshot:
    """查单条 stream 的 info，转成 StreamSnapshot。

    如果 stream 不存在，返回 exists=False 的空快照。
    """
    try:
        from nats.js.errors import NotFoundError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("nats-py JetStream API unavailable") from exc

    try:
        info = await js.stream_info(name)
    except NotFoundError:
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

    cfg = info.config
    state = info.state
    return StreamSnapshot(
        name=name,
        exists=True,
        subjects=list(cfg.subjects or []),
        max_bytes=getattr(cfg, "max_bytes", None),
        max_msgs=getattr(cfg, "max_msgs", None),
        max_msg_size=getattr(cfg, "max_msg_size", None),
        max_age=getattr(cfg, "max_age", None),
        messages=getattr(state, "messages", None),
        bytes=getattr(state, "bytes", None),
    )


# ═════════════════════════════════════════════════════════════════════
# 核心 planning 逻辑（纯函数，易测）
# ═════════════════════════════════════════════════════════════════════


def compute_migration_plan(
    snapshots: dict[str, StreamSnapshot],
    target_specs: tuple[StreamSpec, ...],
    *,
    subject_prefix: str = "aats.",
    purge: bool = False,
    recreate: bool = False,
) -> MigrationPlan:
    """根据 snapshots 和 target_specs 计算 migration plan（不执行任何副作用）。

    Args:
        snapshots: 所有目标 stream 的当前状态快照（包括不存在的）
        target_specs: 目标 StreamSpec 列表（通常是 DEFAULT_STREAM_SPECS）
        subject_prefix: subject 前缀（通常 "aats."）
        purge: 是否额外 purge_stream 每条已存在 stream
        recreate: 是否走 delete + add 全重建模式（会覆盖 purge 语义）

    Returns:
        MigrationPlan：含动作列表 + per-spec 动作分类

    Raises:
        RuntimeError: T6 诡异状态（MARKET 存在但 EVENTS 不存在），带人工恢复步骤
    """
    # ── T6 诡异状态检查 ─────────────────────────────────────
    market_exists = (
        "AATS_EVENTS_MARKET" in snapshots
        and snapshots["AATS_EVENTS_MARKET"].exists
    )
    events_exists = (
        "AATS_EVENTS" in snapshots
        and snapshots["AATS_EVENTS"].exists
    )
    if market_exists and not events_exists and not recreate:
        raise RuntimeError(
            "inconsistent stream state detected:\n"
            "  AATS_EVENTS_MARKET exists but AATS_EVENTS does not\n"
            "  this is a partial-rollback/partial-upgrade state that the\n"
            "  migration script refuses to auto-recover from.\n"
            "\n"
            "  possible causes:\n"
            "    1. a previous migration was interrupted after creating\n"
            "       AATS_EVENTS_MARKET but before touching AATS_EVENTS\n"
            "    2. someone manually deleted AATS_EVENTS for debug purposes\n"
            "    3. bit rot / corruption\n"
            "\n"
            "  manual recovery steps:\n"
            "    1. inspect AATS_EVENTS_MARKET state:\n"
            "       docker exec aats-nats nats stream info AATS_EVENTS_MARKET\n"
            "    2. if its config looks sane, manually create AATS_EVENTS:\n"
            "       python scripts/nats_stream_migrate.py --recreate-events-only\n"
            "       (not implemented; fallback to step 3)\n"
            "    3. full recovery (deletes and recreates both streams):\n"
            "       python scripts/nats_stream_migrate.py --recreate\n"
            "       (purges all data in both streams)\n"
        )

    actions: list[str] = []
    per_spec_actions: dict[str, str] = {}

    for spec in target_specs:
        desired_subjects = [
            f"{subject_prefix}{t}" for t in sorted(spec.topics)
        ]
        snapshot = snapshots.get(spec.name)

        if recreate:
            # 全重建：每条 spec 都 delete + add
            if snapshot and snapshot.exists:
                actions.append(
                    f"[recreate] {spec.name}: delete_stream + add_stream "
                    f"(will lose {snapshot.messages} messages / {snapshot.bytes} bytes)"
                )
            else:
                actions.append(f"[recreate] {spec.name}: add_stream (was missing)")
            per_spec_actions[spec.name] = "recreate"
            continue

        if snapshot is None or not snapshot.exists:
            # T4/T1/T5 分支：不存在 → add_stream
            actions.append(
                f"[add] {spec.name}: create stream "
                f"(subjects={len(desired_subjects)}, "
                f"max_bytes={spec.max_bytes}, max_msgs={spec.max_msgs}, "
                f"max_age={spec.max_age_seconds}s)"
            )
            per_spec_actions[spec.name] = "add"
            continue

        # stream 存在 → 走容量感知 drift 比较
        existing_cfg = _FakeConfigFromSnapshot(snapshot)
        drift = _compute_stream_config_drift(
            existing_cfg, spec, desired_subjects=desired_subjects
        )

        if not drift:
            actions.append(
                f"[noop] {spec.name}: config matches target "
                f"(messages={snapshot.messages}, bytes={snapshot.bytes})"
            )
            per_spec_actions[spec.name] = "noop"
        else:
            drift_summary = ", ".join(sorted(drift.keys()))
            actions.append(
                f"[update] {spec.name}: drift in [{drift_summary}] "
                f"(messages={snapshot.messages}, bytes={snapshot.bytes})"
            )
            per_spec_actions[spec.name] = "update"

    if purge and not recreate:
        existing_names = [
            name for name, snap in snapshots.items() if snap.exists
        ]
        for name in existing_names:
            actions.append(f"[purge] {name}: purge_stream (all data dropped)")

    return MigrationPlan(
        snapshots=snapshots,
        actions=actions,
        per_spec_actions=per_spec_actions,
        will_purge=purge and not recreate,
        will_recreate=recreate,
    )


class _FakeConfigFromSnapshot:
    """把 StreamSnapshot 包装成 nats-py StreamConfig-like 对象用于 drift 比较。

    只需要提供 ``subjects / max_age / max_bytes / max_msgs / max_msg_size``
    几个属性，对齐 ``_compute_stream_config_drift`` 的 getattr 访问模式。
    """

    def __init__(self, snapshot: StreamSnapshot) -> None:
        self.subjects = snapshot.subjects or []
        self.max_age = snapshot.max_age if snapshot.max_age is not None else 0
        self.max_bytes = snapshot.max_bytes if snapshot.max_bytes is not None else 0
        self.max_msgs = snapshot.max_msgs if snapshot.max_msgs is not None else 0
        self.max_msg_size = (
            snapshot.max_msg_size if snapshot.max_msg_size is not None else 0
        )
        self.num_replicas = 1
        self.duplicate_window = 120.0
        self.deny_purge = False


# ═════════════════════════════════════════════════════════════════════
# 执行 migration plan（副作用入口）
# ═════════════════════════════════════════════════════════════════════


async def apply_migration_plan(
    js: Any,
    plan: MigrationPlan,
    target_specs: tuple[StreamSpec, ...],
    *,
    subject_prefix: str = "aats.",
) -> None:
    """按 plan 里的 per_spec_actions 对每条 stream 执行动作。

    T1 特殊顺序（§11.4 subject overlap 陷阱）：EVENTS 和 MARKET 都需要动时，
    **先** update EVENTS 移除 MARKET 的 subjects，**再** add MARKET。
    顺序颠倒会让 nats-py 拒绝 "subjects overlap"。

    实现：按 spec 名字排序不够——需要显式把 "update EVENTS" 放在 "add MARKET" 前。
    做法：两遍遍历：第一遍只执行 update/noop/recreate，第二遍只执行 add。
    """
    # ── 阶段 1: recreate → 先 delete 再 add ────────────────
    if plan.will_recreate:
        for spec in target_specs:
            try:
                await js.delete_stream(spec.name)
                print(f"[exec recreate] deleted {spec.name}")
            except Exception as exc:
                # 可能 stream 本来就不存在 —— 忽略
                print(f"[exec recreate] delete {spec.name} skipped ({type(exc).__name__})")
            desired_cfg = spec.to_nats_stream_config(subject_prefix)
            await js.add_stream(config=desired_cfg)
            print(f"[exec recreate] added {spec.name}")
        return

    # ── 阶段 2a: 先跑 update / noop（让 EVENTS 先剥离 market subjects） ──
    for spec in target_specs:
        action = plan.per_spec_actions.get(spec.name, "noop")
        if action == "update":
            desired_cfg = spec.to_nats_stream_config(subject_prefix)
            await js.update_stream(config=desired_cfg)
            print(f"[exec update] {spec.name}")
        elif action == "noop":
            print(f"[exec noop] {spec.name}")

    # ── 阶段 2b: 再跑 add（MARKET 可以安全声明已经被 EVENTS 释放的 subjects） ──
    for spec in target_specs:
        action = plan.per_spec_actions.get(spec.name, "noop")
        if action == "add":
            desired_cfg = spec.to_nats_stream_config(subject_prefix)
            await js.add_stream(config=desired_cfg)
            print(f"[exec add] {spec.name}")

    # ── 阶段 3: purge（每条已存在 stream） ──────────────────
    if plan.will_purge:
        for name, snap in plan.snapshots.items():
            if snap.exists:
                await js.purge_stream(name)
                print(f"[exec purge] {name}")


# ═════════════════════════════════════════════════════════════════════
# 命令行入口
# ═════════════════════════════════════════════════════════════════════


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NATS AATS events stream migration script (slice nats-capacity)",
    )
    parser.add_argument(
        "--servers",
        default=os.environ.get("NATS_URL", "nats://127.0.0.1:4222"),
        help="NATS server URL（默认从 NATS_URL env 读，fallback nats://127.0.0.1:4222）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只 print plan，不执行任何副作用",
    )
    parser.add_argument(
        "--sync-config",
        action="store_true",
        help="按迁移矩阵执行 add/update；历史数据保留",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="额外对每条已存在 stream purge_stream（仅配合 --sync-config 有效）",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="所有 stream delete_stream + 重建（与 --sync-config / --purge 互斥）",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    """拒绝不合理的 flag 组合。"""
    if args.recreate and args.sync_config:
        raise SystemExit(
            "--recreate and --sync-config are mutually exclusive; "
            "pick one or the other"
        )
    if args.purge and not (args.sync_config or args.dry_run):
        raise SystemExit(
            "--purge only makes sense together with --sync-config (or --dry-run)"
        )
    if not (args.dry_run or args.sync_config or args.recreate):
        raise SystemExit(
            "must specify one of: --dry-run / --sync-config / --recreate"
        )


async def run_migration(args: argparse.Namespace) -> int:
    target_specs = build_nats_streams_from_env(DEFAULT_STREAM_SPECS)

    client, js = await _open_jetstream([args.servers])
    try:
        # ── Step 1: 快照所有目标 stream ────────────────────
        snapshots: dict[str, StreamSnapshot] = {}
        for spec in target_specs:
            snap = await _snapshot_stream(js, spec.name)
            snapshots[spec.name] = snap

        # ── Step 2: 计算 plan（T6 会在这里 raise） ────────
        plan = compute_migration_plan(
            snapshots,
            target_specs,
            purge=args.purge,
            recreate=args.recreate,
        )

        # ── Step 3: print plan ─────────────────────────────
        print("═" * 70)
        print("NATS stream migration plan")
        print("═" * 70)
        for name, snap in plan.snapshots.items():
            if snap.exists:
                print(
                    f"  [{name}] exists: subjects={len(snap.subjects or [])}, "
                    f"max_bytes={snap.max_bytes}, max_msgs={snap.max_msgs}, "
                    f"messages={snap.messages}, bytes={snap.bytes}"
                )
            else:
                print(f"  [{name}] NOT FOUND")
        print()
        print("Actions:")
        for action in plan.actions:
            print(f"  {action}")
        print("═" * 70)

        # ── Step 4: 执行（除非 dry-run） ───────────────────
        if args.dry_run:
            print("dry-run mode: no side effects applied")
            return 0

        await apply_migration_plan(js, plan, target_specs)
        print("migration done")
        return 0
    finally:
        try:
            await client.drain()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_args(args)
    return asyncio.run(run_migration(args))


if __name__ == "__main__":
    sys.exit(main())
