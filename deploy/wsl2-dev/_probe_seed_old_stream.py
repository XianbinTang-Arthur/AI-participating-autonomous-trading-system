"""Test helper: 删除 AATS_EVENTS stream 后用 39 个 subject 重新创建（不含
obligation_updates），模拟 Slice 6.5 之前的旧状态。下一次 ensure_stream 应走
update_stream 路径把 obligation_updates 补上。

仅用于 Slice 6.5 fix(nats-bus) 真跑回归验证。
"""
from __future__ import annotations

import asyncio

import nats  # type: ignore[import-not-found]
from nats.js.api import (  # type: ignore[import-not-found]
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError  # type: ignore[import-not-found]


# Slice 6.5 之前的 39 个 subject —— 和 DEFAULT_CRITICAL_TOPICS 对齐但少一个
# execution.obligation_updates。拷贝自当前 nats_bus.DEFAULT_CRITICAL_TOPICS
# 减去 OBLIGATION_UPDATES 那条，用来模拟旧 stream 状态。
OLD_SUBJECTS = sorted(
    [
        "aats.account.baselines",
        "aats.execution.error_summaries",
        "aats.execution.fill_events",
        # "aats.execution.obligation_updates",  ← Slice 6.5 新增，这里故意漏掉
        "aats.execution.order_intents",
        "aats.execution.order_updates",
        "aats.execution.plans",
        "aats.features.snapshots",
        "aats.market.snapshots",
        "aats.policy.decisions",
        "aats.portfolio.balance_deltas",
        "aats.portfolio.snapshots",
        "aats.reconciliation.reports",
        "aats.reconciliation.validations",
        "aats.replay.validations",
        "aats.risk.decisions",
        "aats.strategy.ai_assessment",
        "aats.strategy.ai_decision_brief",
        "aats.strategy.ai_degradation",
        "aats.strategy.ai_shadow_decision",
        "aats.strategy.ai_shadow_evaluation",
        "aats.strategy.baseline_assessment",
        "aats.strategy.coordinator_snapshots",
        "aats.strategy.decision_context",
        "aats.strategy.decision_outcome",
        "aats.strategy.execution_bundles",
        "aats.strategy.overlay_parent_exposure",
        "aats.strategy.portfolio_allocation_decisions",
        "aats.strategy.position_target",
        "aats.strategy.profile_activation_policies",
        "aats.strategy.profile_activations",
        "aats.strategy.profile_auto_rollback_policies",
        "aats.strategy.profile_recommendations",
        "aats.strategy.profile_rejections",
        "aats.strategy.profile_selection_decisions",
        "aats.strategy.sleeve_intents",
        "aats.system.audit_records",
        "aats.system.kill_switch_state",
        "aats.system.operator_actions",
        "aats.system.processing_failures",
    ]
)


async def main() -> None:
    nc = await nats.connect("nats://nats:4222")
    js = nc.jetstream()
    try:
        try:
            await js.delete_stream("AATS_EVENTS")
            print("[seed] deleted existing AATS_EVENTS")
        except NotFoundError:
            print("[seed] AATS_EVENTS does not exist (fresh)")

        cfg = StreamConfig(
            name="AATS_EVENTS",
            subjects=OLD_SUBJECTS,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            discard=DiscardPolicy.OLD,
            max_age=604800.0,
        )
        await js.add_stream(config=cfg)
        print(
            f"[seed] recreated AATS_EVENTS with {len(OLD_SUBJECTS)} subjects "
            f"(obligation_updates INTENTIONALLY missing)"
        )
        info = await js.stream_info("AATS_EVENTS")
        print(
            f"[seed] verified subject_count={len(info.config.subjects or [])} "
            f"has_obligation_updates="
            f"{'aats.execution.obligation_updates' in (info.config.subjects or [])}"
        )
    finally:
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
