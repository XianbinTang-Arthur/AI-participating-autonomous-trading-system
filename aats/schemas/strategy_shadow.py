"""Round 3 (2026-04-22) · 非 AI 策略的 paper trading shadow schema。

## 定位

和 `ai_shadow.py` 并列，不扩。AI shadow 比 AI 和 baseline；本 schema 比
**候选策略参数** 和 baseline。两者正交，同时存在。

## 用途

用户想调 independent 策略的 entry_threshold / cost_model 等之前，用
真实实盘流量并行跑"改过参数的候选版本"，观察"如果当时决策不同会怎么走"。

Phase 1 只记**决策层**分歧（candidate 相对 baseline 是否会做不同的事）。
Phase 2+ 再算模拟 PnL。

## Schema 设计

`StrategyFamilyShadowDecision` 字段分组:
- Identity: shadow_decision_id / decision_id / symbol / timeframe
- Candidate: candidate_id / candidate_family / candidate_overrides /
  candidate_config_version (sha256 of overrides, 稳定身份)
- Live (baseline): baseline_family / baseline_target_qty / baseline_action
- Shadow: shadow_target_qty / shadow_action
- Divergence: would_override_baseline / shadow_action_type / reason_codes
- Phase 2+ PnL replay: reference_price / reference_spread_bps /
  market_snapshot_ref
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from aats.schemas.ai_shadow import ShadowActionType
from aats.schemas.common import SchemaBase, new_id, utc_now
from aats.schemas.strategy_runtime import StrategyFamily


class StrategyFamilyShadowDecision(SchemaBase):
    """一条候选策略参数在某决策周期里的"如果用它会怎样"快照。

    **Design anchor**（Round 3 design §2）:
    - candidate_id 是人类可读的 "independent_low_threshold" 等
    - candidate_overrides 是原始 dict (for traceability / debugging)
    - candidate_config_version 是 overrides 的 sha256 hex，用于跨
      decision_id 聚合窗口 (同一 config → 同一 version)
    - reference_price / reference_spread_bps / market_snapshot_ref 为 Phase 2+
      的 cheap PnL replay 保留，Phase 1 写入但 evaluator 还不读
    """

    shadow_decision_id: str = Field(default_factory=lambda: new_id("strat_shadow"))
    decision_id: str
    symbol: str
    timeframe: str

    # Candidate identity
    candidate_id: str
    candidate_family: StrategyFamily
    candidate_overrides: dict = Field(default_factory=dict)
    candidate_config_version: str

    # Live (baseline) side
    baseline_family: StrategyFamily
    baseline_target_qty: Decimal
    baseline_action: str

    # Shadow (candidate) side
    shadow_target_qty: Decimal
    shadow_action: str

    # Divergence summary
    would_override_baseline: bool
    shadow_action_type: ShadowActionType  # reuse Literal from ai_shadow.py
    reason_codes: list[str] = Field(default_factory=list)

    # Phase 2+ PnL replay fields (Phase 1 writes best-effort, evaluator 暂不读)
    reference_price: Decimal | None = None
    reference_spread_bps: float | None = None
    market_snapshot_ref: str | None = None

    created_at: datetime = Field(default_factory=utc_now)


class StrategyFamilyShadowEvaluation(SchemaBase):
    """窗口聚合：N 个 decision_id 的 shadow 总结。

    Phase 1 暂不生成（不写入 evaluator），Phase 2 引入后填。
    本 schema 在 Phase 1 定义是为了 forward compatibility：
    跨 phase 的 topic 契约一次性稳定下来。
    """

    evaluation_id: str = Field(
        default_factory=lambda: new_id("strat_shadow_eval")
    )
    window_start: datetime
    window_end: datetime

    symbol: str
    timeframe: str
    candidate_id: str
    candidate_config_version: str
    decision_ids: list[str] = Field(default_factory=list)

    baseline_trade_count: int
    shadow_trade_count: int
    override_count: int
    agreement_count: int
    disagreement_count: int

    # Phase 2+ 接 cheap PnL 时填
    baseline_net_pnl: Decimal | None = None
    shadow_net_pnl: Decimal | None = None
    shadow_outperformed: bool | None = None

    created_at: datetime = Field(default_factory=utc_now)
