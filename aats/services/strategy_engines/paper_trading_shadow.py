"""Round 3 Phase 1.2 · 非 AI 策略的 paper trading shadow 服务。

## 核心思想

给定 live 已经跑出来的 (context, baseline, target)，对每个配置的
`paper_trading_shadow_candidate`：
1. 深拷贝 settings 加上 overrides 得到 candidate_settings
2. 用 candidate_settings 新建一个 TargetPositionEngine
3. 再跑一次 `target_engine.build(context, baseline, ...)` 得到 shadow_target
4. 对比 shadow_target 与 live_target，生成 StrategyFamilyShadowDecision

## 安全不变量

1. **绝不 re-raise 异常**：每个 candidate 的整个评估用 try/except 包，失败
   只 log warning + metric.increment；live path 绝对不受影响。
2. **绝不 mutate `base_settings`**：全部走 `settings.model_copy(update=...)`
   即 Pydantic 不可变拷贝。
3. **engine 实例按 config_version 缓存**：避免每个决策周期重复构造
   TargetPositionEngine。候选 config 很少变（部署期固定），缓存命中率高。

## 不做

- 不模拟 fill / 不做 PnL (Phase 2+)
- 不调 execution layer
- 不动 AI shadow 既有路径
- 不跨 strategy family（当前只支持 override 同 family 的参数）
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.schemas.ai_shadow import ShadowActionType
from aats.schemas.common import utc_now
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.strategy_shadow import StrategyFamilyShadowDecision
from aats.services.decision_engine.target_position import TargetPositionEngine
from aats.services.fee_resolver import EffectiveFeeResolver

# 决策 qty 差异阈值（< 此值视为"实际相同"，容忍浮点误差）。
# 0.001 BTC 是 OKX 最小下单单位，shadow 分歧 < 此值无实际 execution 意义。
_QTY_EPSILON = Decimal("0.0005")


class PaperTradingShadowService:
    """评估一组候选策略参数 vs live baseline 的分歧情况。

    构造参数：
        base_settings: 当前 live settings（作为 override 基础）
        fee_resolver: 共享的 fee resolver（cache 了费率表）
        metrics: MetricsRegistry（递增 paper_trading_shadow_* counters）
        logger: 服务级 logger

    线程安全：本服务假设在单一 event loop 的 `run_cycle` 里同步调用，
    不需要 lock。多线程使用请先加同步层。
    """

    _METRIC_SHADOW_COMPUTED = "paper_trading_shadow_computed_total"
    _METRIC_SHADOW_ERROR = "paper_trading_shadow_errors_total"
    _METRIC_SHADOW_OVERRIDE = "paper_trading_shadow_override_total"

    def __init__(
        self,
        *,
        base_settings: AATSSettings,
        fee_resolver: EffectiveFeeResolver | None = None,
        metrics: MetricsRegistry | None = None,
        logger: Any | None = None,
    ) -> None:
        self._base_settings = base_settings
        self._fee_resolver = fee_resolver or EffectiveFeeResolver(settings=base_settings)
        self._metrics = metrics
        self._logger = logger or get_logger("aats.paper_trading_shadow")
        # engine 缓存：config_version → TargetPositionEngine
        self._engine_cache: dict[str, TargetPositionEngine] = {}

    def enabled(self) -> bool:
        """True 当 paper_trading_shadow_enabled + candidates 非空时。"""
        if not self._base_settings.paper_trading_shadow_enabled:
            return False
        return bool(self._base_settings.paper_trading_shadow_candidates)

    def evaluate_candidates(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        live_target: PositionTarget,
    ) -> list[StrategyFamilyShadowDecision]:
        """对所有配置的候选评估 shadow decision。

        返回 list（可能为空）。任何一个 candidate 的评估异常不影响其他
        candidate 或 live path。
        """
        if not self.enabled():
            return []

        results: list[StrategyFamilyShadowDecision] = []
        for cand in self._base_settings.paper_trading_shadow_candidates:
            try:
                decision = self._evaluate_one(
                    context=context,
                    baseline=baseline,
                    live_target=live_target,
                    candidate=cand,
                )
                if decision is not None:
                    results.append(decision)
                    if self._metrics is not None:
                        self._metrics.increment(self._METRIC_SHADOW_COMPUTED)
                        if decision.would_override_baseline:
                            self._metrics.increment(self._METRIC_SHADOW_OVERRIDE)
            except Exception as exc:
                # **关键不变量**：绝不 re-raise
                if self._metrics is not None:
                    self._metrics.increment(self._METRIC_SHADOW_ERROR)
                log_event(
                    self._logger,
                    "paper_trading_shadow_candidate_failed",
                    level="warning",
                    candidate_id=(cand or {}).get("candidate_id", "<unknown>"),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        return results

    # ──────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────

    def _evaluate_one(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        live_target: PositionTarget,
        candidate: dict,
    ) -> StrategyFamilyShadowDecision | None:
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if not candidate_id:
            log_event(
                self._logger,
                "paper_trading_shadow_candidate_missing_id",
                level="warning",
                candidate_payload=candidate,
            )
            return None

        overrides = dict(candidate.get("overrides") or {})
        candidate_family = candidate.get("family") or self._base_settings.strategy_family_active
        config_version = self._compute_config_version(overrides)

        engine = self._get_or_build_engine(overrides, config_version)

        # 跑 candidate 的 target (operating_mode=baseline_only, 避免触发 AI 代码路径)
        shadow_target = engine.build(
            context=context,
            baseline=baseline,
            ai_assessment=None,
            ai_decision_intent=None,
            operating_mode="baseline_only",
        )

        baseline_qty = Decimal(str(live_target.target_position_qty))
        shadow_qty = Decimal(str(shadow_target.target_position_qty))

        qty_diff = abs(shadow_qty - baseline_qty)
        would_override = qty_diff > _QTY_EPSILON

        return StrategyFamilyShadowDecision(
            decision_id=live_target.decision_id,
            symbol=live_target.symbol,
            timeframe=str(context.timeframe or ""),
            candidate_id=candidate_id,
            candidate_family=candidate_family,
            candidate_overrides=overrides,
            candidate_config_version=config_version,
            baseline_family=live_target.strategy_family,
            baseline_target_qty=baseline_qty,
            baseline_action=_action_label(baseline_qty, live_target.current_position_qty),
            shadow_target_qty=shadow_qty,
            shadow_action=_action_label(shadow_qty, live_target.current_position_qty),
            would_override_baseline=would_override,
            shadow_action_type=_classify_action_type(
                baseline_qty=baseline_qty,
                shadow_qty=shadow_qty,
                current_qty=Decimal(str(live_target.current_position_qty)),
            ),
            reason_codes=[],  # Phase 2+ 加
            created_at=utc_now(),
        )

    def _compute_config_version(self, overrides: dict) -> str:
        """对 overrides 做 sha256，截 16 位 hex。

        稳定身份：同一组 overrides → 同一 config_version，跨 decision 聚合时用。
        """
        canonical = json.dumps(overrides, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def _get_or_build_engine(
        self, overrides: dict, config_version: str
    ) -> TargetPositionEngine:
        """按 config_version 缓存 TargetPositionEngine 实例。"""
        cached = self._engine_cache.get(config_version)
        if cached is not None:
            return cached

        # Pydantic 不可变拷贝：model_copy(update=...) 不修改原 settings
        candidate_settings = self._base_settings.model_copy(update=overrides)
        engine = TargetPositionEngine(
            settings=candidate_settings,
            fee_resolver=None,  # 让它为 candidate settings 重建 fee resolver（不共享）
            metrics=None,  # shadow 不进 live metrics namespace
        )
        self._engine_cache[config_version] = engine
        return engine


# ──────────────────────────────────────────────────────────
# Helpers (module-level, 易测试)
# ──────────────────────────────────────────────────────────


def _action_label(target_qty: Decimal, current_qty: Decimal | float | str) -> str:
    """简化的 action 文本：open_long / open_short / close / reduce / hold。

    不走 strategy_coordinator 复杂逻辑 —— Phase 1 是粗粒度 "会做不同事吗"。
    """
    current = Decimal(str(current_qty))
    diff = target_qty - current
    if abs(diff) <= _QTY_EPSILON:
        return "hold"
    if target_qty == Decimal("0") and current != Decimal("0"):
        return "close"
    if abs(target_qty) < abs(current) and target_qty * current > 0:
        return "reduce"
    if target_qty > 0:
        return "open_long" if current <= 0 else "scale_long"
    if target_qty < 0:
        return "open_short" if current >= 0 else "scale_short"
    return "unknown"


def _classify_action_type(
    *,
    baseline_qty: Decimal,
    shadow_qty: Decimal,
    current_qty: Decimal,
) -> ShadowActionType:
    """对 shadow 和 baseline 的差异分类到 5 种 ShadowActionType。"""
    diff = abs(shadow_qty - baseline_qty)
    if diff <= _QTY_EPSILON:
        return "same_as_baseline"

    baseline_changes = abs(baseline_qty - current_qty) > _QTY_EPSILON
    shadow_changes = abs(shadow_qty - current_qty) > _QTY_EPSILON

    # baseline 想开新仓, shadow 想 hold → hold_instead
    if baseline_changes and not shadow_changes:
        return "hold_instead"

    # baseline hold, shadow 要开/平仓 → entry 或 exit override
    if not baseline_changes and shadow_changes:
        if current_qty == Decimal("0") or abs(shadow_qty) > abs(current_qty):
            return "entry_override"
        return "exit_override"

    # 两边都要改但方向相反（反向覆盖）
    if baseline_qty * shadow_qty < 0:
        return "reverse_override"

    # 两边都要改但同方向、不同幅度 → entry_override
    return "entry_override"
