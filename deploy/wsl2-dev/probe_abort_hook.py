"""Stage 9 checklist-5 — 4 进程真跑 AbortHookService halt drill 驱动脚本。

与 ``probe_kill_switch.py`` 同构：挂进任意一个 AATS 容器内（推荐 gateway），
构造**独立的** AbortHookService + KillSwitch（不复用运行时单例），用手工
注入的 DriftInputs 把状态机推到 halting，然后通过真实 NATS 广播触发 4
个运行时容器的 ``kill_switch_remote_applied``。

验证目标
========
- Stage 9 checklist-4：AbortHookService 的 ``_trigger_halt`` 路径能真正
  调到跨进程 KillSwitch，halt reason 码在 Redis/NATS 广播里正确落盘：
  - ``stage9_abort_hook:score_ge_5``（critical）
  - ``stage9_abort_hook:subscore_financial_2``（单类 critical）
  - ``stage9_abort_hook:score_3_4_consecutive_2``（连续 warning）
- 设计文档 §5、§6：halting → cooldown → monitoring 状态机在真跑环境下
  与 operator resume 的交互
- 本脚本使用 ``source_role=stage9_probe``，运行时容器把它当外部事件 apply，
  和 probe_kill_switch.py 的模式完全一致。

用法（容器内）::

    # 1) 纯 in-memory 自检（不碰 Redis/NATS），确认 service 与 drift_score
    #    配线正常。跑这个不会动任何 4 进程的状态。
    PYTHONPATH=/app python /tmp/probe_abort_hook.py self-check

    # 2) 真跑 halt drill（critical score_ge_5）。执行后 4 个运行时容器
    #    应该 halted=true。记得接着跑 resume 收尾。
    PYTHONPATH=/app python /tmp/probe_abort_hook.py halt-critical

    # 3) 真跑 halt drill（subscore_financial_2，只有财务子类 critical）
    PYTHONPATH=/app python /tmp/probe_abort_hook.py halt-subscore-financial

    # 4) 真跑 halt drill（连续 warning，score=4 两次）
    PYTHONPATH=/app python /tmp/probe_abort_hook.py halt-consecutive

    # 5) resume drill（清理上面 halt 造成的 halted=true 状态）
    PYTHONPATH=/app python /tmp/probe_abort_hook.py resume

    # 6) Redis 里当前 abort_hook 视角下的 kill_switch 状态
    PYTHONPATH=/app python /tmp/probe_abort_hook.py status

⚠️ 本脚本不动运行时进程里**自己**那个 AbortHookService 实例。那个实例
订阅的是 runtime 的 inputs_provider，按自己节奏 evaluate。probe 的作用
是：扮演第 5 个临时 sidecar，证明"只要一个 sidecar 决定 halt，4 进程
都会应用 halt 广播" —— halt → NATS 广播的这条链路。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# 同 probe_kill_switch.py：确保能 import aats.*
# runtime 容器里 /app 是 repo 根，PYTHONPATH=/app 已经设好。
from aats.bus.nats_bus import NatsBusConfig, NatsEventBus
from aats.events import topics
from aats.services.governance_engine.abort_hooks import (
    AbortHookConfig,
    AbortHookService,
)
from aats.services.governance_engine.drift_score import DriftInputs, compute_drift_score
from aats.services.governance_engine.kill_switch import (
    KILL_SWITCH_REDIS_KEY,
    KillSwitch,
)
from aats.storage.event_store import InMemoryEventStore
from aats.storage.hot_state_store import RedisHotStateConfig, RedisHotStateStore


_PROCESS_ROLE = "stage9_probe"


# ─────────────────────────────────────────────────────────────────────
# 样例 DriftInputs 构造器（从单测里 mirror 过来）
# ─────────────────────────────────────────────────────────────────────


def _critical_inputs() -> DriftInputs:
    """全 critical，total=8，abort_hook_action=halt_immediate。"""
    return DriftInputs(
        stage="T2",
        window_hours=24,
        evaluated_at=datetime.now(timezone.utc),
        balance_drift_ratio=Decimal("0.10"),
        max_drawdown_ratio=Decimal("0.08"),
        fee_to_pnl_ratio=Decimal("0.80"),
        fill_success_ratio=Decimal("0.85"),
        adverse_slippage_ratio=Decimal("0.20"),
        decision_cycle_cadence_ratio=Decimal("0.50"),
        decision_error_ratio=Decimal("0.10"),
        reconciliation_mismatch_count=5,
        nats_handler_error_ratio=Decimal("0.05"),
        okx_rate_limit_count=10,
        notes=["probe: critical total_score=8"],
    )


def _financial_critical_inputs() -> DriftInputs:
    """仅 financial 子类全 critical (subscore=2)，其他 clean。total=3 但
    abort_hook_action=halt_immediate（subscore=2 规则覆盖）。"""
    return DriftInputs(
        stage="T2",
        window_hours=24,
        evaluated_at=datetime.now(timezone.utc),
        balance_drift_ratio=Decimal("0.10"),
        max_drawdown_ratio=Decimal("0.08"),
        fee_to_pnl_ratio=Decimal("0.80"),
        fill_success_ratio=Decimal("0.99"),
        adverse_slippage_ratio=Decimal("0.005"),
        decision_cycle_cadence_ratio=Decimal("0.99"),
        decision_error_ratio=Decimal("0.001"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0001"),
        okx_rate_limit_count=0,
        notes=["probe: financial subscore=2"],
    )


def _warning_inputs() -> DriftInputs:
    """total=4，action=halt_on_repeat。需要连续两次才 halt。"""
    return DriftInputs(
        stage="T2",
        window_hours=24,
        evaluated_at=datetime.now(timezone.utc),
        balance_drift_ratio=Decimal("0.03"),
        max_drawdown_ratio=Decimal("0.04"),
        fee_to_pnl_ratio=Decimal("0.45"),
        fill_success_ratio=Decimal("0.93"),
        adverse_slippage_ratio=Decimal("0.05"),
        decision_cycle_cadence_ratio=Decimal("0.85"),
        decision_error_ratio=Decimal("0.03"),
        reconciliation_mismatch_count=2,
        nats_handler_error_ratio=Decimal("0.005"),
        okx_rate_limit_count=3,
        notes=["probe: warning total_score=4"],
    )


# ─────────────────────────────────────────────────────────────────────
# 真 Redis+NATS 依赖装配（与 probe_kill_switch.py 对齐）
# ─────────────────────────────────────────────────────────────────────


async def _build_real_kill_switch() -> tuple[KillSwitch, RedisHotStateStore, NatsEventBus]:
    redis_url = os.environ.get("AATS_HOT_STATE_REDIS_URL", "redis://redis:6379/0")
    nats_url = os.environ.get("AATS_NATS_URL", "nats://nats:4222")
    print(f"[probe] redis={redis_url} nats={nats_url}", flush=True)

    store = RedisHotStateStore(RedisHotStateConfig(url=redis_url))
    await store.connect()

    bus = NatsEventBus(
        config=NatsBusConfig(servers=[nats_url]),
        event_store=InMemoryEventStore(),
        persistence_mode="best_effort",
        consumer_role=_PROCESS_ROLE,
    )
    await bus.connect()

    ks = KillSwitch()
    logger = logging.getLogger("probe.abort_hook.kill_switch")
    await ks.bootstrap(
        hot_state_store=store,
        bus=bus,
        process_role=_PROCESS_ROLE,
        logger=logger,
    )
    print(f"[probe] kill_switch bootstrap done, initial halted={ks.halted}", flush=True)
    return ks, store, bus


def _make_service_with_provider(
    *,
    kill_switch: KillSwitch,
    provider,
    consecutive: int = 2,
) -> AbortHookService:
    return AbortHookService(
        config=AbortHookConfig(
            enabled=True,
            evaluate_interval_seconds=60.0,
            consecutive_warning_threshold=consecutive,
            cooldown_seconds=1800.0,
        ),
        kill_switch=kill_switch,
        inputs_provider=provider,
        logger=logging.getLogger("probe.abort_hook"),
    )


# ─────────────────────────────────────────────────────────────────────
# Command: self-check（纯 in-memory，不碰 Redis/NATS）
# ─────────────────────────────────────────────────────────────────────


async def _cmd_self_check() -> int:
    """用一个内存 KillSwitch 在本地跑一次 compute_drift_score +
    AbortHookService 状态机，证明：
    - compute_drift_score 的导入与计算路径正常
    - AbortHookService 能真正 evaluate 并转换状态
    - _trigger_halt 路径里 kill_switch.halt 调得上

    不碰 Redis、不碰 NATS，纯进程内。适合在每次部署前冒烟。
    """
    ks = KillSwitch()
    current = {"inputs": _critical_inputs()}
    service = _make_service_with_provider(
        kill_switch=ks,
        provider=lambda: current["inputs"],
    )
    report = await service.evaluate_once()
    assert report is not None, "evaluate_once returned None"
    print(
        f"[probe] self-check: total_score={report.total_score} "
        f"state={report.state} action={report.abort_hook_action}",
        flush=True,
    )
    snap = service.snapshot()
    print(f"[probe] self-check: state={snap.state} halts={snap.halts_triggered}", flush=True)
    if snap.state != "halting":
        print("[probe] FAIL: critical inputs did not reach halting", flush=True)
        return 1
    if not ks.halted:
        print("[probe] FAIL: KillSwitch.halted is False after halting transition", flush=True)
        return 1
    reason = ks.status().get("reason")
    print(f"[probe] self-check: kill_switch reason={reason!r}", flush=True)
    if not reason or not reason.startswith("stage9_abort_hook:"):
        print("[probe] FAIL: halt reason does not carry stage9_abort_hook prefix", flush=True)
        return 1
    print("[probe] OK: self-check passed", flush=True)
    return 0


# ─────────────────────────────────────────────────────────────────────
# Command: halt-critical / halt-subscore-financial / halt-consecutive
# ─────────────────────────────────────────────────────────────────────


async def _run_halt_drill(
    *,
    label: str,
    build_inputs_sequence,
) -> int:
    """通用 halt drill：
    1. 构造真实 Redis + NATS + bootstrap 的 KillSwitch
    2. 创建 AbortHookService，用 ``build_inputs_sequence`` 注入 inputs
       并连续 evaluate_once
    3. 读回 Redis 里的 kill_switch 状态，断言 halted=true
    4. Redis/NATS close，本地 service 不需要 stop（没 start 过 _loop）

    本脚本不会自动 resume —— 跑完记得 ``resume`` 命令收尾。
    """
    ks, store, bus = await _build_real_kill_switch()
    if ks.halted:
        print(
            "[probe] WARNING: kill_switch already halted (probably from a previous drill). "
            "Run 'resume' first before running a new halt drill.",
            flush=True,
        )
        await bus.close()
        await store.close()
        return 2

    # 状态机需要可变的 inputs；把它们封到 mutable cell
    inputs_ref: dict[str, DriftInputs] = {"current": build_inputs_sequence[0]}
    service = _make_service_with_provider(
        kill_switch=ks,
        provider=lambda: inputs_ref["current"],
        consecutive=2,
    )

    print(f"[probe] {label}: start drill, {len(build_inputs_sequence)} evaluate cycle(s)", flush=True)
    for idx, inputs in enumerate(build_inputs_sequence):
        inputs_ref["current"] = inputs
        report = await service.evaluate_once()
        snap = service.snapshot()
        print(
            f"[probe] {label}: cycle {idx + 1} "
            f"score={report.total_score if report else None} "
            f"state={snap.state} "
            f"consecutive={snap.consecutive_warning_count}",
            flush=True,
        )

    raw: Any = await store.get(KILL_SWITCH_REDIS_KEY)
    print(f"[probe] {label}: redis readback: {raw}", flush=True)
    await bus.close()
    await store.close()

    if not isinstance(raw, dict) or not raw.get("halted"):
        print(f"[probe] {label}: FAIL — redis did not reflect halt", flush=True)
        return 1
    reason = raw.get("reason") or ""
    if not str(reason).startswith("stage9_abort_hook:"):
        print(
            f"[probe] {label}: FAIL — halt reason should start with 'stage9_abort_hook:' "
            f"but is {reason!r}",
            flush=True,
        )
        return 1
    print(f"[probe] {label}: OK — halt persisted + broadcast (reason={reason!r})", flush=True)
    print(
        f"[probe] {label}: 现在检查 4 个运行时容器日志应该看到 "
        f"'kill_switch_remote_applied' + source_role={_PROCESS_ROLE}",
        flush=True,
    )
    return 0


async def _cmd_halt_critical() -> int:
    return await _run_halt_drill(
        label="halt-critical",
        build_inputs_sequence=[_critical_inputs()],
    )


async def _cmd_halt_subscore_financial() -> int:
    return await _run_halt_drill(
        label="halt-subscore-financial",
        build_inputs_sequence=[_financial_critical_inputs()],
    )


async def _cmd_halt_consecutive() -> int:
    # 连续 2 次 warning → halt
    return await _run_halt_drill(
        label="halt-consecutive",
        build_inputs_sequence=[_warning_inputs(), _warning_inputs()],
    )


# ─────────────────────────────────────────────────────────────────────
# Command: resume / status（复用 probe_kill_switch.py 的模式）
# ─────────────────────────────────────────────────────────────────────


async def _cmd_resume() -> int:
    ks, store, bus = await _build_real_kill_switch()
    if not ks.halted:
        print("[probe] resume: already not halted, no-op", flush=True)
        await bus.close()
        await store.close()
        return 0
    await ks.resume_async()
    print(f"[probe] resume: local halted={ks.halted}", flush=True)
    raw: Any = await store.get(KILL_SWITCH_REDIS_KEY)
    print(f"[probe] resume: redis readback: {raw}", flush=True)
    await bus.close()
    await store.close()
    if isinstance(raw, dict) and raw.get("halted"):
        print("[probe] resume: FAIL — redis still halted", flush=True)
        return 1
    print("[probe] resume: OK", flush=True)
    return 0


async def _cmd_status() -> int:
    redis_url = os.environ.get("AATS_HOT_STATE_REDIS_URL", "redis://redis:6379/0")
    store = RedisHotStateStore(RedisHotStateConfig(url=redis_url))
    await store.connect()
    raw: Any = await store.get(KILL_SWITCH_REDIS_KEY)
    print(f"[probe] redis kill_switch state: {raw}", flush=True)
    await store.close()

    # 再跑一次 compute_drift_score 打印当前默认（all-missing）报告，
    # 帮助 operator 看"如果现在真跑 evaluate，会得到什么 score"
    # 这里不去读 runtime 里的 inputs_provider —— 纯粹当 CLI 用
    inputs = DriftInputs(
        stage=os.environ.get("AATS_STAGE9_CURRENT_STAGE", "T0"),  # type: ignore[arg-type]
        window_hours=24,
        evaluated_at=datetime.now(timezone.utc),
    )
    report = compute_drift_score(inputs)
    print(
        f"[probe] compute_drift_score (all-missing baseline): "
        f"total={report.total_score} state={report.state} "
        f"action={report.abort_hook_action} allow_upgrade={report.allow_ladder_upgrade}",
        flush=True,
    )
    return 0


# ─────────────────────────────────────────────────────────────────────
# main dispatch
# ─────────────────────────────────────────────────────────────────────


_COMMANDS = {
    "self-check": _cmd_self_check,
    "halt-critical": _cmd_halt_critical,
    "halt-subscore-financial": _cmd_halt_subscore_financial,
    "halt-consecutive": _cmd_halt_consecutive,
    "resume": _cmd_resume,
    "status": _cmd_status,
}


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        print(
            "usage: probe_abort_hook.py <"
            + "|".join(_COMMANDS.keys())
            + ">",
            file=sys.stderr,
        )
        return 2
    return await _COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
