"""Active Parameter Set 加载器.

主交易系统启动时读取 active parameter set，
将 RDP 治理层产出的研究参数注入 family/timeframe 配置。

参数优先级（从低到高）:
    hardcoded defaults
      < strategy_profiles/*.yaml
      < active parameter set          ← 本模块负责
      < runtime emergency override

唯一数据来源: governance.active_parameter_sets (PostgreSQL)
治理层 (governance.active_decisions) 控制参数启用/暂停。

API:
  load_active_parameter_registry(db_url) -> dict
  get_active_parameters(registry, family, timeframe) -> dict
  merge_active_parameters(base_params, active_params) -> dict
  build_settings_overrides(...) -> dict[str, Any]
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
    resolve_governance_db_url,
)
from aats.storage.connection_budget import ACTIVE_PARAMETER_TRANSIENT_POOL

log = logging.getLogger(__name__)


class ActiveParameterSafetyError(RuntimeError):
    """Active-parameter truth cannot be applied without runtime/DB divergence."""


def _approval_timestamp_valid(value: Any) -> bool:
    if not isinstance(value, datetime):
        return False
    # PostgreSQL timestamptz is an absolute instant, but psycopg renders it in
    # the session timezone.  A +08 aware datetime is therefore just as valid as
    # the equivalent +00 value; only naive datetimes are ambiguous.
    if value.tzinfo is None or value.utcoffset() is None:
        return False
    return value.astimezone(timezone.utc) <= (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    )


def _release_apply_timestamp_valid(
    applied_at_value: Any,
    created_at_value: Any,
) -> bool:
    """Validate the payload audit instant against the canonical release row."""
    if not isinstance(applied_at_value, str):
        return False
    token = applied_at_value.strip()
    if not token or not (token.endswith("Z") or token.endswith("+00:00")):
        return False
    try:
        applied_at = datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return False
    if applied_at.tzinfo is None or applied_at.utcoffset() != timedelta(0):
        return False
    if not isinstance(created_at_value, datetime):
        return False
    if created_at_value.tzinfo is None or created_at_value.utcoffset() is None:
        return False
    created_at = created_at_value.astimezone(timezone.utc)
    return created_at <= applied_at <= (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    )

# ── 已知的 family × timeframe 组合 ─────────────────────────────────

KNOWN_COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1h"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1h"},
]

# ── RDP 参数 → 主系统设置字段的映射 ────────────────────────────────
#
# ⚠️  语义映射说明（P1 review item）
#
# 本映射表定义了 RDP 研究层参数名 → 主系统 AATSSettings 字段名的对应关系。
# 每条映射标注了映射类型:
#   [DIRECT]      — 同义映射，RDP 参数与生产字段描述同一概念
#   [APPROXIMATE]  — 近似映射，RDP 参数语义接近但不完全等同生产字段
#   [PLACEHOLDER] — 第一版占位，需后续确认语义是否准确
#
# 修改此映射前，必须同时更新:
#   1. docs/operations/parameter_mapping_reference.md
#   2. 确认 RDP 研究层计算该参数时使用的单位与生产端一致
#
# ────────────────────────────────────────────────────────────────────

PARAMETER_MAPPING_INDEPENDENT: dict[str, str] = {
    # ════════════════════════════════════════════════════════════════
    # RDP 研究层核心参数 — 自动映射到生产端
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] signal_edge_scale_bps → strategy_signal_edge_scale_bps
    #
    # RDP 含义: composite_score → bps 的缩放系数 (signal_edge = score * scale)
    # 生产端: 在 compute_signal_edge_bps() 中启用 score-based 信号边际路径。
    #         当该值被注入后，生产端同时计算:
    #           path1: alpha * strategy_alpha_edge_bps_scale + bonuses (组件路径)
    #           path2: composite_score * strategy_signal_edge_scale_bps (RDP 路径)
    #         最终 signal_edge = max(path1, path2)
    #
    # 历史: 曾错误映射到 de_risk_net_edge_bps（scale=15 导致开了就关）。
    #       现映射到专用新字段 strategy_signal_edge_scale_bps，语义完全一致。
    #
    # RDP Phase 2 验证 (120 天 BTC-USDT-SWAP):
    #   scale=20 → independent/15m: pos_ratio=97.5%, net_edge=+3.89bps
    #   scale=20 → independent/1h:  pos_ratio=98.3%, net_edge=+4.91bps
    "signal_edge_scale_bps": "strategy_signal_edge_scale_bps",

    # [DIRECT] score_stability_threshold → min_score_stability_bps
    #
    # 历史: 此前因 bps 单位不一致（RDP ×10000 vs 生产 ×100）而被排除。
    # 修复: RDP replay 端已统一为 ×100 (independent_adapter.py line 295)。
    # 现在两端语义一致: score_stability_threshold=5.0 → 允许回撤 5/100=0.05
    #
    # ⚠️ 优先级关系（与 min_score_drawdown_bps 的交互）:
    #   score_stability_threshold → strategy_hedge_independent_min_score_stability_bps (base)
    #   min_score_drawdown_bps    → strategy_hedge_independent_min_score_drawdown_bps (override)
    #
    #   生产端 effective_score_drawdown_threshold_bps() 的判定顺序:
    #     1. 若 min_score_drawdown_bps is not None → 使用 drawdown (override 优先)
    #     2. 否则 → fallback 到 min_score_stability_bps (base)
    #
    #   当 active set 同时包含两个参数时, drawdown 优先,
    #   score_stability_threshold 仅作为 fallback 兜底值。
    "score_stability_threshold": "strategy_hedge_independent_min_score_stability_bps",

    # ════════════════════════════════════════════════════════════════
    # 原有映射（Phase 2）
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] RDP 回测优化的最小确认 tick 数
    # → 生产端 independent hedge 的确认 tick 数
    # 单位一致: tick count; 语义: 信号确认所需的最少 tick
    "min_confirm_ticks": "strategy_hedge_independent_min_confirm_ticks",

    # [DIRECT] RDP 回测的最小安全净边际 (bps)
    # → 生产端 independent hedge 的最小安全净边际 (bps)
    # 单位一致: bps; 语义: 交易执行的净边际安全线
    "min_safe_net_edge_bps": "strategy_hedge_independent_min_safe_net_edge_bps",

    # ════════════════════════════════════════════════════════════════
    # Phase 1 扩展：进出场阈值
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] 开仓评分阈值（long book）
    # 单位一致: ratio 0~1; 语义: 评分达到此阈值才允许开多仓
    "entry_threshold": "strategy_hedge_independent_long_entry_threshold",

    # [DIRECT] 平仓评分阈值（long book）
    # 单位一致: ratio 0~1; 语义: 评分低于此阈值触发平多仓
    "close_threshold": "strategy_hedge_independent_long_close_threshold",

    # [DIRECT] 加仓评分阈值（long book）
    # 单位一致: ratio 0~1; 语义: 评分达到此阈值才允许多头加仓
    # ⚠️ REPLAY 未模拟: replay 只有 open/hold/close 三态，无 scale-in 逻辑
    #    该参数仅透传到生产端，RDP 回测不验证其效果
    "scale_in_threshold": "strategy_hedge_independent_long_scale_in_threshold",

    # [DIRECT] 开仓评分阈值（short book，非对称设置）
    # 单位一致: ratio 0~1; 语义: 评分达到此阈值才允许开空仓
    "short_entry_threshold": "strategy_hedge_independent_short_entry_threshold",

    # [DIRECT] 平仓评分阈值（short book，非对称设置）
    # 单位一致: ratio 0~1; 语义: 评分低于此阈值触发平空仓
    "short_close_threshold": "strategy_hedge_independent_short_close_threshold",

    # ════════════════════════════════════════════════════════════════
    # Phase 1 扩展：持仓时间管理
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] 最小持仓秒数
    # 单位一致: seconds; 语义: 防止过频交易
    # ⚠️ 仅映射 long 方向。如需 short 方向独立配置，需新增
    #    short_min_hold_seconds → strategy_hedge_independent_short_min_hold_seconds
    "min_hold_seconds": "strategy_hedge_independent_long_min_hold_seconds",

    # [DIRECT] 平仓后冷却秒数
    # 单位一致: seconds; 语义: 平仓后一段时间不开新仓
    "rebalance_cooldown_seconds": "strategy_hedge_independent_rebalance_cooldown_seconds",

    # [DIRECT] thesis 最长存活秒数
    # 单位一致: seconds; 语义: 超过此时间允许按 stale 退出
    "max_thesis_age_seconds": "strategy_hedge_independent_max_thesis_age_seconds",

    # ════════════════════════════════════════════════════════════════
    # Phase 1 扩展：风险管理阈值
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] 降风险触发阈值
    # 单位一致: bps; 语义: 净边际变薄时触发降风险
    "de_risk_net_edge_bps": "strategy_hedge_independent_de_risk_net_edge_bps",

    # [DIRECT] thesis 失效阈值
    # 单位一致: bps; 语义: 净边际低于此值视为 thesis 失效
    # 约束: 必须 <= de_risk_net_edge_bps
    "failed_thesis_net_edge_bps": "strategy_hedge_independent_failed_thesis_net_edge_bps",

    # [DIRECT] 灾难性 failed_thesis 缓冲（whipsaw 防护）
    # 单位一致: bps; 语义: 仅当 net_edge <= failed_thesis_threshold - 此缓冲 时
    # 判定为灾难性失效，允许豁免 min_hold 立即出场。
    # 默认 3.0 bps 以吸收正常行情抖动，避免标准 failed_thesis 引发 whipsaw。
    "catastrophic_failed_thesis_buffer_bps": "strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps",

    # ════════════════════════════════════════════════════════════════
    # Phase 1 扩展：成本缓冲
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] 开仓预期滑点缓冲
    # 单位一致: bps; 语义: 叠加到成本估算中
    "expected_slippage_buffer_bps": "strategy_hedge_independent_expected_slippage_buffer_bps",

    # [DIRECT] 开仓执行缓冲
    # 单位一致: bps; 语义: 叠加到成本估算中
    "expected_execution_buffer_bps": "strategy_hedge_independent_expected_execution_buffer_bps",

    # [DIRECT] 最大允许单边成本
    # 单位一致: bps; 语义: 超出则阻断开仓
    "max_acceptable_cost_bps": "strategy_hedge_independent_max_acceptable_cost_bps",

    # ════════════════════════════════════════════════════════════════
    # Phase 1 扩展：评分质量
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] 评分最大回撤容忍度
    # 单位一致: bps; 语义: 评分波动超过此值则视为不稳定
    "min_score_drawdown_bps": "strategy_hedge_independent_min_score_drawdown_bps",

    # [APPROXIMATE] 最低流动性质量分
    # 单位一致: ratio 0~1; 语义: replay 默认 liq=1.0，此参数做灵敏度分析
    # ⚠️ replay 不模拟真实流动性，此映射仅传递阈值到生产端
    "min_liquidity_quality": "strategy_hedge_independent_min_liquidity_quality",

    # ════════════════════════════════════════════════════════════════
    # Phase 1 扩展：执行策略
    # ════════════════════════════════════════════════════════════════

    # [DIRECT] 开仓限价偏移
    # 单位一致: bps; 语义: bounded-limit IOC 的价格偏移
    # ⚠️ REPLAY 未模拟: causal harness 禁止同 bar 成交，但仍无 offset 订单簿匹配模型
    #    该参数仅透传到生产端，RDP 回测不验证其效果
    "limit_offset_bps_entry": "strategy_hedge_independent_limit_offset_bps_entry",
}

PARAMETER_MAPPING_DIRECTIONAL: dict[str, str] = {
    # ════════════════════════════════════════════════════════════════
    # directional 家族 — 生产端直接消费字段
    # ════════════════════════════════════════════════════════════════
    #
    # 注意：directional 不使用 `strategy_hedge_independent_*` overlay 的
    # 专属字段（rebalance_cooldown / max_thesis_age / de_risk_net_edge 等），
    # 那些字段仅对 independent family 生效。directional 的决策走
    # target_position.py 的 `_baseline_target_qty()` → `_qty_from_bias()` 路径，
    # 只消费 global 作用域的 strategy_* 字段。
    #
    # 因此 RDP 对 directional 输出的其它研究参数（entry_threshold / close_threshold
    # 等）当前仅供 DirectionalReplayAdapter 回测使用，在生产端无生效位点，
    # 不应强制映射（见 build_settings_overrides 的 per-family required 逻辑）。

    # [DIRECT] 最小持仓秒数（directional 与 independent 共用 global 字段）
    # 生产端消费点: target_position.py L1407/1413 self.settings.strategy_min_hold_seconds
    # 单位一致: seconds; 语义: 防止过频交易
    "min_hold_seconds": "strategy_min_hold_seconds",

    # ⚠️ 不映射 directional_trend_weight → strategy_entry_alpha_min
    #
    # 历史上此处有一个 PLACEHOLDER 映射 (已撤除):
    #   "directional_trend_weight": "strategy_entry_alpha_min"
    #
    # 撤除原因 (2026-04-18 实盘发现):
    #   - RDP 端 directional_trend_weight ∈ [0, 1], 典型值 0.7~1.0
    #   - 生产端 strategy_entry_alpha_min 是"入场 alpha 最低阈值",
    #     AI alpha ∈ [-1, 1], 默认 0.17 (profile=0.1)
    #   - 语义完全不对等: "趋势信号的权重" ≠ "入场 alpha 最低阈值"
    #   - 注入 1.0 会导致全家族 (含 independent) 入场门控全面锁死,
    #     因为 strategy_entry_alpha_min 是 target_position._trade_thresholds()
    #     的 global 门槛, 所有 family 走统一入口
    #
    # directional_trend_weight 已在 _RDP_REPLAY_ONLY_PARAMS 白名单中,
    # DirectionalReplayAdapter 在回测时消费, 生产端应静默忽略.
    #
    # 如果未来策略层确定 trend_weight 与 alpha_min 的数学关系, 或新增
    # directional 专属字段 (e.g. strategy_hedge_directional_entry_alpha_min),
    # 再重新评估映射.

    # [DIRECT] RDP 回测使用的 taker 手续费 → 生产端衍生品 taker 费
    # 单位一致: bps; 语义: 同一概念
    "taker_fee_bps": "trade_cost_derivatives_taker_fee_bps",

    # [DIRECT] RDP 回测使用的滑点估计 → 生产端衍生品滑点
    # 单位一致: bps; 语义: 同一概念
    "slippage_bps": "trade_cost_derivatives_slippage_bps",
}

FAMILY_PARAMETER_MAPPINGS: dict[str, dict[str, str]] = {
    "independent": PARAMETER_MAPPING_INDEPENDENT,
    "directional": PARAMETER_MAPPING_DIRECTIONAL,
}

# ── RDP 研究参数的 "per-family required 集合"，用于检测映射缺失 ──────
#
# 每个 family 有各自必须映射到 AATSSettings 的子集。只有 required 子集
# 里的 key 在 FAMILY_PARAMETER_MAPPINGS 中缺失时，build_settings_overrides
# 才会记录 ERROR 并 skip 该 combo（fail-close）。非 required 且无映射的
# key 视为 "RDP 回测专用参数"（仅供 replay adapter 消费），降级到 INFO
# 记录被 dropped，避免在 family 间的语义差异引发日志污染。
#
# 设计依据：
#   - independent family 完整消费 21 个研究 key（含 independent-hedge-overlay
#     的全部字段），任何缺失都是真实的"研究输出未接入生产"断链
#   - directional family 的决策路径走 baseline.direction_bias → _qty_from_bias,
#     不使用 _hedge_independent_* overlay 字段；只有 min_hold_seconds 能直接
#     被 target_position.py 消费（其他研究 key 仅对 DirectionalReplayAdapter
#     的 score 模型有效，生产端暂无生效位点）
#   - 其余 family（如 smart_arbitrage）当前未纳入 RDP，required 留空
#
# 非 required 且未映射的研究 key ≠ "漏接"，而是"研究层比生产层多维度"，
# 这是 RDP 设计的正常状态。
_RDP_CORE_RESEARCH_PARAMS_BY_FAMILY: dict[str, frozenset[str]] = {
    "independent": frozenset({
        "entry_threshold",
        "close_threshold",
        "scale_in_threshold",
        "short_entry_threshold",
        "short_close_threshold",
        "min_hold_seconds",
        "rebalance_cooldown_seconds",
        "max_thesis_age_seconds",
        "de_risk_net_edge_bps",
        "failed_thesis_net_edge_bps",
        "catastrophic_failed_thesis_buffer_bps",
        "expected_slippage_buffer_bps",
        "expected_execution_buffer_bps",
        "max_acceptable_cost_bps",
        "min_score_drawdown_bps",
        "min_liquidity_quality",
        "limit_offset_bps_entry",
        "signal_edge_scale_bps",
        "score_stability_threshold",
        "min_confirm_ticks",
        "min_safe_net_edge_bps",
    }),
    "directional": frozenset({
        # 当前 directional 生产决策路径只消费这一个 RDP 研究 key
        # （见 target_position.py L1407/1413）。其它 RDP 研究 key
        # 由 DirectionalReplayAdapter 在回测内部使用，不经生产端。
        "min_hold_seconds",
    }),
    # 其他 family（smart_arbitrage / spot_grid / dca）当前未纳入 RDP pipeline，
    # required 留空。
}

# ── 兼容别名 ────────────────────────────────────────────────────
#
# 外部引用（包括历史测试）仍通过 `_RDP_CORE_RESEARCH_PARAMS` 访问 independent
# 家族的规范 key 集合。为避免 breaking change，保留此别名。
_RDP_CORE_RESEARCH_PARAMS: frozenset[str] = _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY[
    "independent"
]

# ── 参数白名单: 即使映射缺失，这些 key 也不会触发 WARNING ─────────
#
# 某些 key 不属于 "RDP 层需要注入生产端" 的范畴，例如:
#   - cost_config / taker_fee_bps / slippage_bps: 仅供 replay 成本模型
#   - directional_trend_weight / directional_return_clamp_bps: 仅供 replay adapter
#   - strategy_short_bias_enabled: 目标 profile 的 replay 上下文快照；生产端是
#     global 能力开关，不能由多个 family/timeframe active set 竞争覆盖
#
# 这些参数由 ReplayParameterOverrides 消费，无需透传到 AATSSettings。
_RDP_REPLAY_ONLY_PARAMS: frozenset[str] = frozenset({
    "cost_config",
    "taker_fee_bps",
    "slippage_bps",
    "directional_trend_weight",
    "directional_return_clamp_bps",
    "strategy_short_bias_enabled",
})

# ── 默认路径（兼容常量，外部调用方仍引用） ─────────────────────────

DEFAULT_ACTIVE_DIR = "configs/active_parameter_sets"
DEFAULT_REGISTRY_FILENAME = "active_parameter_registry.json"


# ══════════════════════════════════════════════════════════════════
#  DB 数据源
# ══════════════════════════════════════════════════════════════════


def _try_load_from_db(db_url: str | None = None) -> dict[str, Any] | None:
    """尝试从数据库加载 active parameter registry.

    同时查询 ``governance.active_decisions`` 与 release effectiveness：
    - 每个 active combo 必须有 allowlist 决策，缺失/无效/pause 均隔离；
    - 任何曾产生 ``rollback_triggered`` 的 immutable parameter set 均隔离；
    - 决策授权键由 canonical ``family + lower(timeframe)`` 构造，不信任旧
      ``combo_key`` 文本。

    Returns
    -------
    dict | None  成功时返回 registry dict，失败或不可用时返回 None。
    """
    # 与所有 governance writer 共用同一 resolver，避免 writer 通过
    # RDP_DATABASE_URL 写入、bootstrap 却只看专用变量而静默跑默认参数。
    url = db_url or resolve_governance_db_url()
    if not url:
        return None

    try:
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=ACTIVE_PARAMETER_TRANSIENT_POOL.pool_size,
            max_overflow=ACTIVE_PARAMETER_TRANSIENT_POOL.max_overflow,
        )
        try:
            with engine.connect() as conn, conn.begin():
                # 三张治理表必须来自同一 MVCC 快照。PostgreSQL 默认的
                # READ COMMITTED 会让每条 SELECT 看到不同提交点；并发 apply
                # 可因此把旧 active set 与新 decision 拼成一个从未真实存在过的
                # 启动配置。只读 REPEATABLE READ 把整个授权视图固定在同一时刻。
                conn.execute(sa_text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                ))
                rows = conn.execute(sa_text(
                    "SELECT a.family, a.timeframe, a.parameter_set_id, "
                    "a.values AS param_values, a.source_round_id, "
                    "a.approval_recommendation_id, a.applied_by, a.applied_at, "
                    "p.parameter_set_id AS canonical_parameter_set_id, "
                    "p.family AS parameter_set_family, "
                    "p.symbol AS parameter_set_symbol, "
                    "p.timeframe AS parameter_set_timeframe, "
                    "p.values AS canonical_param_values, "
                    "p.status AS parameter_set_status, "
                    "p.source_round_id AS parameter_set_source_round_id, "
                    "r.recommendation_id AS canonical_recommendation_id, "
                    "r.family AS recommendation_family, "
                    "r.symbol AS recommendation_symbol, "
                    "r.timeframe AS recommendation_timeframe, "
                    "r.target_parameter_set_id, "
                    "r.source_round_id AS recommendation_source_round_id, "
                    "r.recommendation_type, r.status AS recommendation_status, "
                    "r.evidence_bundle_ref, r.approved_by, r.approved_at, "
                    "rel.release_id AS canonical_release_id, "
                    "rel.release_count, rel.apply_operation_id, "
                    "rel.release_applied_at, rel.release_created_at, "
                    "h.lineage_count "
                    "FROM governance.active_parameter_sets AS a "
                    "LEFT JOIN governance.parameter_sets AS p "
                    "ON p.parameter_set_id = a.parameter_set_id "
                    "LEFT JOIN governance.recommendations AS r "
                    "ON r.recommendation_id = a.approval_recommendation_id "
                    "LEFT JOIN LATERAL ("
                    "  SELECT pr.release_id, pr.created_at AS release_created_at, "
                    "         pr.payload ->> 'apply_operation_id' AS apply_operation_id, "
                    "         pr.payload ->> 'applied_at' AS release_applied_at, "
                    "         COUNT(*) OVER () AS release_count "
                    "  FROM governance.parameter_releases AS pr "
                    "  WHERE pr.apply_result = 'success' "
                    "    AND pr.parameter_set_id = a.parameter_set_id "
                    "    AND pr.recommendation_id = a.approval_recommendation_id "
                    "    AND lower(btrim(pr.family)) = lower(btrim(a.family)) "
                    "    AND lower(btrim(pr.timeframe)) = lower(btrim(a.timeframe)) "
                    "  ORDER BY pr.created_at DESC, pr.release_id DESC "
                    "  LIMIT 1"
                    ") AS rel ON TRUE "
                    "LEFT JOIN LATERAL ("
                    "  SELECT COUNT(*) AS lineage_count "
                    "  FROM governance.parameter_apply_history AS ah "
                    "  WHERE ah.operation_type = 'apply' "
                    "    AND ah.operation_id = rel.apply_operation_id "
                    "    AND ah.to_parameter_set_id = a.parameter_set_id "
                    "    AND ah.recommendation_id = a.approval_recommendation_id "
                    "    AND lower(btrim(ah.family)) = lower(btrim(a.family)) "
                    "    AND lower(btrim(ah.timeframe)) = lower(btrim(a.timeframe))"
                    ") AS h ON TRUE "
                    "ORDER BY a.family, a.timeframe"
                )).fetchall()

                # ── 查询治理决策状态 ──
                # active_parameter_sets 与 active_decisions 必须作为同一份风险
                # 视图读取。任何一个查询失败都让整个 registry 失败关闭；否则
                # 表级权限或瞬时错误会把数据库里的 pause 误解释成“没有暂停”。
                governance_managed = True
                paused_combos: set[str] = set()
                decision_statuses: dict[str, list[Any]] = {}
                decision_rows = conn.execute(sa_text(
                    "SELECT family, timeframe, combo_key, current_status "
                    "FROM governance.active_decisions"
                )).fetchall()
                for dr in decision_rows:
                    decision_combo = (
                        f"{str(dr.family).strip().lower()}_"
                        f"{str(dr.timeframe).strip().lower()}"
                    )
                    decision_status = dr.current_status
                    decision_statuses.setdefault(decision_combo, []).append(
                        decision_status
                    )
                    if decision_status == "pause":
                        paused_combos.add(decision_combo)

                known_bad_rows = conn.execute(sa_text(
                    "WITH risk_evidence AS ("
                    "  SELECT e.release_id, e.family, e.timeframe, "
                    "         e.payload ->> 'combo_key' AS risk_combo_key "
                    "  FROM governance.release_effectiveness AS e "
                    "  WHERE e.conclusion = 'rollback_triggered' "
                    "  UNION ALL "
                    "  SELECT o.release_id, o.family, o.timeframe, o.combo_key "
                    "  FROM governance.observation_results AS o "
                    "  WHERE o.status = 'rollback_recommended' "
                    "  UNION ALL "
                    "  SELECT rb.release_id, rb.family, rb.timeframe, rb.combo_key "
                    "  FROM governance.rollback_recommendations AS rb "
                    "  WHERE rb.rollback_recommended IS TRUE"
                    ") "
                    "SELECT risk.release_id, risk.family AS risk_family, "
                    "risk.timeframe AS risk_timeframe, "
                    "risk.risk_combo_key, r.parameter_set_id, "
                    "r.family AS release_family, "
                    "r.timeframe AS release_timeframe, "
                    "r.combo_key AS release_combo_key, r.apply_result "
                    "FROM risk_evidence AS risk "
                    "LEFT JOIN governance.parameter_releases AS r "
                    "ON r.release_id = risk.release_id"
                )).fetchall()
                # parameter_set_id 是 immutable/FK identity。不能再依赖旧 release
                # 上可能为空、大小写漂移或被污染的 family/timeframe 来“洗白”
                # 一个已经触发 rollback 的参数集。
                known_bad_sets: set[str] = set()
                global_risk_reconciliation = False
                for bad in known_bad_rows:
                    bad_parameter_set_id = str(
                        getattr(bad, "parameter_set_id", "") or ""
                    ).strip()
                    release_family = str(
                        getattr(bad, "release_family", "") or ""
                    ).strip().lower()
                    release_timeframe = str(
                        getattr(bad, "release_timeframe", "") or ""
                    ).strip().lower()
                    release_combo = str(
                        getattr(bad, "release_combo_key", "") or ""
                    ).strip().lower()
                    risk_family = str(
                        getattr(bad, "risk_family", "") or ""
                    ).strip().lower()
                    risk_timeframe = str(
                        getattr(bad, "risk_timeframe", "") or ""
                    ).strip().lower()
                    risk_combo = str(
                        getattr(bad, "risk_combo_key", "") or ""
                    ).strip().lower()
                    risk_lineage_valid = bool(
                        str(getattr(bad, "release_id", "") or "").strip()
                        and bad_parameter_set_id
                        and getattr(bad, "apply_result", None) == "success"
                        and release_family
                        and release_timeframe
                        and release_combo
                        and risk_family == release_family
                        and risk_timeframe == release_timeframe
                        and risk_combo == release_combo
                        and release_combo
                        == f"{release_family}_{release_timeframe}"
                    )
                    if not risk_lineage_valid:
                        global_risk_reconciliation = True
                        continue
                    known_bad_sets.add(bad_parameter_set_id)
        finally:
            engine.dispose()

        active_sets: dict[str, Any] = {}
        quarantined: dict[str, str] = {}
        seen_active_combos: set[str] = set()
        allowed_decision_statuses = {
            "keep_active",
            "lower_priority",
            "require_review",
        }
        for row in rows:
            family = str(row.family).strip().lower()
            timeframe = str(row.timeframe).strip().lower()
            combo_key = f"{family}_{timeframe}"
            if combo_key in seen_active_combos:
                quarantined[combo_key] = "duplicate_canonical_active_set"
                active_sets.pop(combo_key, None)
                continue
            seen_active_combos.add(combo_key)
            if global_risk_reconciliation:
                quarantined[combo_key] = "global_risk_evidence_lineage_invalid"
                active_sets.pop(combo_key, None)
                continue
            decision_values = decision_statuses.get(combo_key, [])
            if len(decision_values) != 1:
                quarantined[combo_key] = (
                    "decision_missing_or_ambiguous:"
                    f"count={len(decision_values)}"
                )
                active_sets.pop(combo_key, None)
                continue
            decision_status = decision_values[0]
            if decision_status not in allowed_decision_statuses:
                quarantined[combo_key] = (
                    "decision_missing_or_not_apply_capable:"
                    f"{decision_status if isinstance(decision_status, str) else 'invalid_type'}"
                )
                active_sets.pop(combo_key, None)
                continue
            if str(row.parameter_set_id).strip() in known_bad_sets:
                quarantined[combo_key] = "known_bad_parameter_set"
                active_sets.pop(combo_key, None)
                continue
            active_parameter_set_id = str(row.parameter_set_id or "").strip()
            active_source_round_id = str(row.source_round_id or "").strip()
            active_recommendation_id = str(
                row.approval_recommendation_id or ""
            ).strip()
            lineage_valid = bool(
                active_parameter_set_id
                and active_source_round_id
                and active_recommendation_id
                and getattr(row, "canonical_parameter_set_id", None)
                == active_parameter_set_id
                and str(getattr(row, "parameter_set_family", "") or "")
                .strip()
                .lower()
                == family
                and str(getattr(row, "parameter_set_timeframe", "") or "")
                .strip()
                .lower()
                == timeframe
                and str(getattr(row, "parameter_set_symbol", "") or "").strip()
                and getattr(row, "parameter_set_symbol", None)
                == getattr(row, "recommendation_symbol", None)
                and getattr(row, "canonical_param_values", None)
                == row.param_values
                and str(
                    getattr(row, "parameter_set_source_round_id", "") or ""
                ).strip()
                == active_source_round_id
                and getattr(row, "canonical_recommendation_id", None)
                == active_recommendation_id
                and str(getattr(row, "recommendation_family", "") or "")
                .strip()
                .lower()
                == family
                and str(getattr(row, "recommendation_timeframe", "") or "")
                .strip()
                .lower()
                == timeframe
                and getattr(row, "target_parameter_set_id", None)
                == active_parameter_set_id
                # recommendation 的 decision/evidence round 与 parameter set
                # 的 generation round 是不同血缘；两者都必须存在，但绝不能
                # 错误强制相等。
                and str(getattr(row, "recommendation_source_round_id", "") or "")
                .strip()
                and str(getattr(row, "evidence_bundle_ref", "") or "").strip()
                and getattr(row, "recommendation_type", None)
                == "parameter_upgrade"
                and getattr(row, "recommendation_status", None)
                in {"approved", "superseded"}
                and str(getattr(row, "approved_by", "") or "").strip()
                and _approval_timestamp_valid(
                    getattr(row, "approved_at", None)
                )
                and getattr(row, "parameter_set_status", None) == "released"
                and str(
                    getattr(row, "canonical_release_id", "") or ""
                ).strip()
                and int(getattr(row, "release_count", 0) or 0) == 1
                and str(getattr(row, "apply_operation_id", "") or "").strip()
                and _release_apply_timestamp_valid(
                    getattr(row, "release_applied_at", None),
                    getattr(row, "release_created_at", None),
                )
                and int(getattr(row, "lineage_count", 0) or 0) == 1
            )
            if not lineage_valid:
                quarantined[combo_key] = "active_parameter_lineage_invalid"
                active_sets.pop(combo_key, None)
                continue
            if type(row.param_values) is not dict:
                quarantined[combo_key] = "active_parameter_values_not_object"
                active_sets.pop(combo_key, None)
                continue
            active_sets[combo_key] = {
                "parameter_set_id": active_parameter_set_id,
                "family": family,
                "timeframe": timeframe,
                "values": deepcopy(row.param_values),
                "source_round_id": active_source_round_id,
                "approval_recommendation_id": active_recommendation_id,
                "applied_by": row.applied_by,
                "applied_at": (
                    row.applied_at.isoformat()
                    if getattr(row.applied_at, "isoformat", None)
                    else row.applied_at
                ),
            }

        # Validate the reverse edge as well.  Driving the registry only from
        # active_parameter_sets makes a missing/partially migrated active row
        # indistinguishable from an intentional pause and silently restores
        # profile defaults.  Every decision that can keep runtime trading must
        # own exactly one canonical active row in the same snapshot.  Only an
        # explicit, unambiguous ``pause`` decision is allowed without one.
        for combo_key, decision_values in decision_statuses.items():
            if combo_key in seen_active_combos:
                continue
            if len(decision_values) != 1:
                quarantined[combo_key] = (
                    "decision_without_active_set_ambiguous:"
                    f"count={len(decision_values)}"
                )
                continue
            decision_status = decision_values[0]
            if decision_status in allowed_decision_statuses:
                quarantined[combo_key] = (
                    "apply_capable_decision_missing_active_set"
                )
            elif decision_status != "pause":
                quarantined[combo_key] = (
                    "decision_without_active_set_invalid_status:"
                    f"{decision_status if isinstance(decision_status, str) else 'invalid_type'}"
                )

        if not decision_statuses and not seen_active_combos:
            quarantined["__governance__"] = "managed_decision_state_empty"

        if quarantined:
            log.warning(
                "active_parameter_governance_quarantine: 隔离 %d 个 combo: %s",
                len(quarantined),
                ", ".join(
                    f"{combo}={reason}"
                    for combo, reason in sorted(quarantined.items())
                ),
            )
        log.info(
            "从数据库加载 active parameter registry (%d active sets, governance_managed=%s, paused=%d)",
            len(active_sets), governance_managed, len(paused_combos),
        )
        return {
            "generated_at": None,
            "active_sets": active_sets,
            "governance_managed": governance_managed,
            "paused_combos": sorted(paused_combos),
            "quarantined_combos": quarantined,
        }

    except Exception as exc:
        # DB 连接/查询失败。调用方会根据是否存在 managed DB 配置决定：
        # managed runtime 必须 fail-closed；纯离线开发才可保留 profile 默认值。
        log.error(
            "active_parameter_db_load_failed: 数据库加载 active parameters 失败，"
            "无法取得 canonical governance truth。err=%s",
            exc,
        )
        return None


def load_active_parameter_registry(
    _path: Path | str | None = None,
    *,
    project_root: Path | str | None = None,
    db_url: str | None = None,
    skip_db: bool = False,
) -> dict[str, Any]:
    """加载 active parameter registry.

    唯一数据来源: PostgreSQL governance.active_parameter_sets。
    显式 managed DB 不可用时返回 ``db_load_failed``，启动注入路径必须
    fail-closed；只有未配置 governance DB 的离线开发环境可使用默认配置。

    Parameters
    ----------
    _path : deprecated, ignored
        历史遗留参数，保留签名兼容性，不再使用。
    skip_db : bool
        为 True 时跳过 DB 路径，返回空 registry。用于 DB partial
        fallback 场景——避免在 AATS_ACTIVE_PARAMETER_DB_URL 已设置的
        环境中再次读到同一份 partial DB 结果。

    格式::

        {
          "generated_at": "...",
          "active_sets": {
            "independent_15m": {
              "parameter_set_id": "ps_xxx",
              "family": "independent",
              "timeframe": "15m",
              "values": { ... }
            },
            ...
          }
        }
    """
    if _path is not None:
        log.debug(
            "load_active_parameter_registry: path 参数已弃用，忽略 (got %s)", _path,
        )

    # skip_db=True 时直接返回空（用于 partial fallback 的文件补齐场景）
    if skip_db:
        return {"generated_at": None, "active_sets": {}}

    db_result = _try_load_from_db(db_url)
    if db_result is not None:
        return db_result

    # DB 不可用时需区分 "没配置 DB"（离线开发）和 managed truth outage。
    # has_explicit... 覆盖 AATS_ACTIVE_PARAMETER_DB_URL、RDP_DATABASE_URL、
    # 非默认 settings URL 与项目 .env.research marker，但不会读取/打印凭证。
    explicit_db = bool(str(db_url or "").strip())
    managed_db = explicit_db or has_explicit_governance_db_configuration(
        Path(project_root) if project_root is not None else None
    )
    if managed_db:
        log.error(
            "active_parameter_registry_degraded: DB URL 已配置但加载失败，"
            "拒绝回退 profile 默认参数。",
        )
        return {
            "generated_at": None,
            "active_sets": {},
            "governance_managed": True,
            "db_load_failed": True,
            "quarantined_combos": {},
        }
    log.info("active parameter registry: DB 未配置，返回空 registry（使用默认配置）")
    return {"generated_at": None, "active_sets": {}}


def get_active_parameters(
    registry: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    """从 registry 获取指定 combo 的参数值.

    如果不存在，返回空 dict（fallback 到原始配置）。
    """
    combo_key = f"{family}_{timeframe.lower()}"
    entry = registry.get("active_sets", {}).get(combo_key)
    if entry is None:
        return {}
    return entry.get("values", {})


def merge_active_parameters(
    base_params: dict[str, Any],
    active_params: dict[str, Any],
) -> dict[str, Any]:
    """合并 active 参数到 base 参数.

    active 覆盖 base，未提供的字段 fallback 原值。
    """
    merged = dict(base_params)
    for key, val in active_params.items():
        if val is not None:
            merged[key] = val
    return merged


def save_active_parameter_registry(
    registry: dict[str, Any],
    path: Path | str | None = None,
    *,
    project_root: Path | str | None = None,
) -> Path:
    """保存 active_parameter_registry.json（原子写入）.

    .. deprecated:: JSON 文件已不再是数据来源，保留供外部调用方过渡期使用。
    """
    from aats.data_platform.governance._atomic_io import atomic_json_write

    if path is None:
        root = Path(project_root) if project_root else Path(".")
        path = root / DEFAULT_ACTIVE_DIR / DEFAULT_REGISTRY_FILENAME
    else:
        path = Path(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(registry, path)
    log.info("已保存 active parameter registry -> %s", path)
    return path


# ══════════════════════════════════════════════════════════════════
#  写入函数（保留供外部调用方过渡期使用）
# ══════════════════════════════════════════════════════════════════


def write_active_parameter_set(
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict[str, Any],
    source_round_id: str | None = None,
    approval_recommendation_id: str | None = None,
    applied_by: str = "manual",
    project_root: Path | str | None = None,
) -> Path:
    """写入一个 active parameter set（per-file 模式）.

    .. deprecated:: JSON 文件已不再是数据来源，保留供外部调用方过渡期使用。
    """
    from aats.data_platform.governance._atomic_io import atomic_json_write

    root = Path(project_root) if project_root else Path(".")
    active_dir = root / DEFAULT_ACTIVE_DIR
    active_dir.mkdir(parents=True, exist_ok=True)

    combo_key = f"{family}_{timeframe.lower()}"
    file_path = active_dir / f"{combo_key}.json"

    data = {
        "meta": {
            "parameter_set_id": parameter_set_id,
            "family": family,
            "timeframe": timeframe,
            "status": "active",
            "source_round_id": source_round_id,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "applied_by": applied_by,
            "approval_recommendation_id": approval_recommendation_id,
        },
        "values": dict(values),
    }

    atomic_json_write(data, file_path)
    log.info("已写入 active parameter set: %s -> %s", combo_key, file_path)
    return file_path


def upsert_active_registry(
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict[str, Any],
    project_root: Path | str | None = None,
) -> Path:
    """更新或插入 active_parameter_registry.json 中的一个 combo.

    .. deprecated:: JSON 文件已不再是数据来源，保留供外部调用方过渡期使用。
    """
    registry = load_active_parameter_registry(project_root=project_root)

    combo_key = f"{family}_{timeframe.lower()}"
    registry.setdefault("active_sets", {})[combo_key] = {
        "parameter_set_id": parameter_set_id,
        "family": family,
        "timeframe": timeframe,
        "values": dict(values),
    }

    return save_active_parameter_registry(registry, project_root=project_root)


def load_all_active_parameter_sets(
    *,
    project_root: Path | str | None = None,
    skip_db: bool = False,
) -> dict[str, dict[str, Any]]:
    """加载所有 active parameter sets（DB-only）.

    Parameters
    ----------
    skip_db : bool
        传递给 ``load_active_parameter_registry``。为 True 时返回空 dict，
        避免在 AATS_ACTIVE_PARAMETER_DB_URL 已设置的环境中再次读到同一份
        partial DB 结果。
    """
    registry = load_active_parameter_registry(
        project_root=project_root, skip_db=skip_db,
    )
    active_sets = registry.get("active_sets", {})
    if not active_sets:
        return {}

    # 转换为与 per-file 兼容的格式
    result: dict[str, dict[str, Any]] = {}
    for combo_key, entry in active_sets.items():
        meta: dict[str, Any] = {
            "parameter_set_id": entry.get("parameter_set_id", ""),
            "family": entry.get("family", ""),
            "timeframe": entry.get("timeframe", ""),
            "status": "active",
        }
        # 传递 recalibration 标记（如存在）
        if entry.get("recalibration_needed"):
            meta["recalibration_needed"] = True
            meta["recalibration_reason"] = entry.get(
                "recalibration_reason", "unknown"
            )
            log.warning(
                "active parameter set %s 标记为需要重新校准 (reason: %s)，"
                "请重新运行 RDP pipeline 以获取修正后的参数",
                combo_key,
                meta["recalibration_reason"],
            )
        result[combo_key] = {
            "meta": meta,
            "values": entry.get("values", {}),
        }
    return result


# ══════════════════════════════════════════════════════════════════
#  主系统 settings 集成
# ══════════════════════════════════════════════════════════════════


def get_active_parameter_values(
    family: str,
    timeframe: str,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """获取指定 combo 的参数值（仅 values 部分）."""
    all_sets = load_all_active_parameter_sets(project_root=project_root)
    combo_key = f"{family}_{timeframe.lower()}"
    data = all_sets.get(combo_key)
    if data is None:
        return {}
    return data.get("values", {})


def build_settings_overrides(
    *,
    project_root: Path | str | None = None,
    registry_path: Path | str | None = None,
    db_url: str | None = None,
    families: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    """构建可合并到 AATSSettings 的参数覆盖 dict.

    根据 FAMILY_PARAMETER_MAPPINGS 将 RDP 参数名
    映射为主系统设置字段名。

    这是 active parameter → settings 注入的核心函数。
    在 build_runtime() 中被调用。

    唯一数据来源: DB (db_url / AATS_ACTIVE_PARAMETER_DB_URL)。

    Parameters
    ----------
    registry_path : deprecated, ignored
        历史遗留参数，保留签名兼容性，不再使用。
    """
    if registry_path is not None:
        log.debug(
            "build_settings_overrides: registry_path 参数已弃用，忽略 (got %s)",
            registry_path,
        )

    # 统一走 load_active_parameter_registry（DB-only）
    registry = load_active_parameter_registry(
        project_root=project_root, db_url=db_url,
    )
    all_sets_raw = registry.get("active_sets", {})
    quarantined = registry.get("quarantined_combos", {})
    if registry.get("db_load_failed") is True:
        raise ActiveParameterSafetyError(
            "active parameter DB truth unavailable; refusing profile-default fallback"
        )
    if isinstance(quarantined, dict) and quarantined:
        details = ", ".join(
            f"{combo}={reason}"
            for combo, reason in sorted(quarantined.items())
        )
        raise ActiveParameterSafetyError(
            "active parameter governance quarantine requires reconciliation: "
            f"{details}"
        )

    # 治理层是否已接管参数管理（DB 查询时同步获取）。managed truth 不能容忍
    # partial/default merge：每个生产可消费字段都必须来自一份完整、可验证的
    # immutable parameter set；否则启动时直接失败关闭。
    governance_managed = registry.get("governance_managed") is True

    if all_sets_raw:
        all_sets: dict[str, dict[str, Any]] = {}
        for k, v in all_sets_raw.items():
            all_sets[k] = {"values": v.get("values", {})}
    elif governance_managed:
        # ── 治理层已接管且结果为空 → 所有 combo 均被暂停/未启用 ──
        paused_combos: set[str] = set(registry.get("paused_combos", []))
        log.info(
            "active_parameter_governance_all_paused: "
            "治理层已接管参数管理，当前所有 combo 均被暂停 (%s)，"
            "策略将使用代码默认参数。",
            ", ".join(sorted(paused_combos)) if paused_combos else "无明确 pause 记录",
        )
        all_sets = {}
    else:
        all_sets = {}

    if not all_sets:
        return {}

    overrides: dict[str, Any] = {}
    # AATSSettings is currently a flat runtime contract: it has no timeframe
    # dimension.  Two active combos may therefore map to the same field.  A
    # last-writer-wins merge would make runtime behaviour depend on DB row
    # ordering while claiming that both parameter sets were applied.  Keep the
    # first writer and fail closed when another combo proposes a different
    # value.  Identical values are safe and preserve both lineage identifiers.
    override_sources: dict[str, tuple[Any, str]] = {}
    applied_combos: list[str] = []
    # combo_key -> required 子集中缺映射的 key（fail-close 场景,ERROR 级）
    missing_required_by_combo: dict[str, list[str]] = {}
    # combo_key -> 非 required 且未映射的研究 key（"研究层多维度",INFO 级）
    dropped_optional_by_combo: dict[str, list[str]] = {}

    for combo_key, data in all_sets.items():
        parts = combo_key.rsplit("_", 1)
        if len(parts) != 2:
            if governance_managed:
                raise ActiveParameterSafetyError(
                    f"managed active parameter combo key is invalid: {combo_key!r}"
                )
            continue
        family, timeframe = parts

        if families and family not in families:
            continue
        if timeframes and timeframe.lower() not in [t.lower() for t in timeframes]:
            continue

        if governance_managed and (
            family not in FAMILY_PARAMETER_MAPPINGS
            or family not in _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY
        ):
            raise ActiveParameterSafetyError(
                f"managed active parameter family has no production contract: {family!r}"
            )
        mapping = FAMILY_PARAMETER_MAPPINGS.get(family, {})
        required = _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY.get(family, frozenset())
        values = data.get("values", {})
        if type(values) is not dict:
            raise ActiveParameterSafetyError(
                f"managed active parameter values must be an object: {combo_key}"
            )

        production_mapped = set(mapping).difference(_RDP_REPLAY_ONLY_PARAMS)
        required_production_mapped = production_mapped
        if governance_managed:
            missing_contract_values = sorted(
                required_production_mapped.difference(values)
            )
            missing_mapping = sorted(required.difference(mapping))
            if missing_contract_values or missing_mapping:
                details: list[str] = []
                if missing_contract_values:
                    details.append(
                        "missing_values=" + ",".join(missing_contract_values)
                    )
                if missing_mapping:
                    details.append(
                        "missing_mapping=" + ",".join(missing_mapping)
                    )
                raise ActiveParameterSafetyError(
                    f"managed active parameter contract incomplete [{combo_key}]: "
                    + "; ".join(details)
                )

        # ── 分三类：required 缺映射 / 非 required 未映射 / 已映射 ──
        missing_required: list[str] = []
        dropped_optional: list[str] = []
        for rdp_param in values.keys():
            if rdp_param in mapping:
                continue  # 已被映射
            if rdp_param in _RDP_REPLAY_ONLY_PARAMS:
                continue  # 属于 replay-only, 不应映射
            if rdp_param in required:
                missing_required.append(rdp_param)
            else:
                dropped_optional.append(rdp_param)

        if missing_required:
            missing_required_by_combo[combo_key] = sorted(missing_required)
            log.error(
                "Active parameter combo skipped [%s]: required research params "
                "are not mapped to AATSSettings (family=%s). Missing keys: %s. "
                "Check FAMILY_PARAMETER_MAPPINGS['%s'] and "
                "_RDP_CORE_RESEARCH_PARAMS_BY_FAMILY['%s'].",
                combo_key,
                family,
                ", ".join(sorted(missing_required)),
                family,
                family,
            )
            continue

        if dropped_optional:
            dropped_optional_by_combo[combo_key] = sorted(dropped_optional)

        mapped_value_applied = False
        for rdp_param, settings_field in mapping.items():
            if rdp_param not in values:
                continue
            if values[rdp_param] is None:
                if governance_managed and rdp_param in required_production_mapped:
                    raise ActiveParameterSafetyError(
                        "managed active parameter contains null production value "
                        f"[{combo_key}]: {rdp_param}"
                    )
                log.warning(
                    "Active parameter skipped None value [%s]: %s",
                    combo_key,
                    rdp_param,
                )
                continue
            if governance_managed:
                from pydantic import TypeAdapter, ValidationError

                from aats.bootstrap.settings import AATSSettings

                settings_model_field = AATSSettings.model_fields.get(settings_field)
                if settings_model_field is None:
                    raise ActiveParameterSafetyError(
                        "managed active parameter maps to unknown AATSSettings field "
                        f"[{combo_key}]: {rdp_param}->{settings_field}"
                    )
                try:
                    TypeAdapter(settings_model_field.annotation).validate_python(
                        values[rdp_param], strict=True
                    )
                except ValidationError as exc:
                    raise ActiveParameterSafetyError(
                        "managed active parameter value failed strict settings validation "
                        f"[{combo_key}]: {rdp_param}->{settings_field}"
                    ) from exc
            candidate_value = values[rdp_param]
            previous = override_sources.get(settings_field)
            if previous is not None and previous[0] != candidate_value:
                previous_value, previous_combo = previous
                raise ActiveParameterSafetyError(
                    "active parameter settings collision: flat runtime field "
                    f"{settings_field!r} receives conflicting values from "
                    f"{previous_combo!r} ({previous_value!r}) and "
                    f"{combo_key!r} ({candidate_value!r}); enable only one "
                    "timeframe or introduce a timeframe-aware runtime contract"
                )
            overrides[settings_field] = candidate_value
            override_sources.setdefault(
                settings_field,
                (candidate_value, combo_key),
            )
            mapped_value_applied = True

        if mapped_value_applied:
            applied_combos.append(combo_key)
        elif governance_managed:
            raise ActiveParameterSafetyError(
                f"managed active parameter produced no settings overrides: {combo_key}"
            )

    if applied_combos:
        overrides["active_parameter_set_ids"] = {
            combo_key: str(all_sets_raw[combo_key].get("parameter_set_id") or "")
            for combo_key in applied_combos
            if str(all_sets_raw[combo_key].get("parameter_set_id") or "").strip()
        }
        log.info(
            "Active parameter overrides: %d fields from %s",
            len(overrides),
            ", ".join(applied_combos),
        )

    # 非 required 未映射的研究 key：降级 INFO 记录（不是断链,是"研究层比
    # 生产层多维度"的正常状态；保留日志以便排查 RDP 输出的实际范围）
    for combo_key, dropped in dropped_optional_by_combo.items():
        family, _, _timeframe = combo_key.partition("_")
        log.info(
            "Active parameter research-only keys [%s]: %d keys in research output "
            "not required for production (family=%s). Dropped: %s. "
            "These keys are consumed by RDP replay adapter only.",
            combo_key,
            len(dropped),
            family,
            ", ".join(dropped),
        )

    return overrides


def _coerce_float_setting(
    settings: dict[str, Any],
    key: str,
    default: float,
) -> float:
    value = settings.get(key, default)
    if value is None:
        return float(default)
    return float(value)


def apply_active_parameters_to_settings(
    resolved_settings: dict[str, Any],
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """将 active parameters 合并到已解析的 settings dict.

    用于在 build_runtime() 中注入 active parameters。
    Fail-soft: 加载失败时打 warning，返回原 settings 不修改。

    Parameters
    ----------
    resolved_settings : dict
        profile_resolution.resolved_settings
    project_root : Path, optional
        项目根目录

    Returns
    -------
    dict  合并后的 settings dict
    """
    # 检查主开关
    enabled = resolved_settings.get("active_parameters_enabled", False)
    if not enabled:
        return resolved_settings

    registry_path = resolved_settings.get("active_parameter_registry_path")
    db_url = resolved_settings.get("active_parameter_db_url")

    try:
        # P1-2 fix: 传入 enabled_decision_timeframes 以过滤非活跃时间框架。
        # 否则当 DB / 文件中同时包含 15m 和 1h 参数集时，后加载的 1h 参数
        # 会覆盖当前实盘使用的 15m 参数（因为映射目标字段相同）。
        active_timeframes = resolved_settings.get("enabled_decision_timeframes")
        # settings 里该字段是 tuple[str,...], 转为 list 供 filter
        if active_timeframes is not None and not isinstance(active_timeframes, list):
            active_timeframes = list(active_timeframes)
        overrides = build_settings_overrides(
            project_root=project_root,
            registry_path=registry_path,
            db_url=db_url,
            timeframes=active_timeframes,
        )
    except ActiveParameterSafetyError:
        raise
    except Exception as exc:
        log.warning(
            "Active parameter 加载失败（fallback 原配置）: %s", exc,
        )
        return resolved_settings

    if not overrides:
        log.info("Active parameter: 无覆盖项")
        return resolved_settings

    # 合并
    merged = dict(resolved_settings)
    for key, val in overrides.items():
        if key in merged:
            log.info("Active parameter override: %s = %s (was %s)", key, val, merged[key])
        merged[key] = val

    # ── 结构性不变量校验 ──────────────────────────────────────────
    _validate_safe_edge_invariant(merged)

    return merged


def _validate_safe_edge_invariant(settings: dict[str, Any]) -> None:
    """校验 safe_edge > de_risk_net_edge_bps 不变量。

    若违反则记录 ERROR 级日志，不中断启动（fail-soft），但确保
    运维人员能第一时间发现配置倒挂。
    """
    min_safe = max(_coerce_float_setting(
        settings,
        "strategy_hedge_independent_min_safe_net_edge_bps",
        2.0,
    ), 0.0)
    slippage_buf = max(_coerce_float_setting(
        settings,
        "strategy_hedge_independent_expected_slippage_buffer_bps",
        0.5,
    ), 0.0)
    exec_buf = max(_coerce_float_setting(
        settings,
        "strategy_hedge_independent_expected_execution_buffer_bps",
        0.5,
    ), 0.0)
    de_risk = _coerce_float_setting(
        settings,
        "strategy_hedge_independent_de_risk_net_edge_bps",
        2.0,
    )
    safe_edge = min_safe + slippage_buf + exec_buf
    # 要求 safe_edge >= de_risk + 1.0 bps 最小 hysteresis 带
    if safe_edge < de_risk + 1.0:
        log.error(
            "⚠️ 配置倒挂: safe_edge(%.1f = %.1f + %.1f + %.1f) "
            "< de_risk_net_edge_bps(%.1f) + 1.0 bps，持仓 hysteresis 带过窄。"
            "请提高 min_safe_net_edge_bps 或降低 de_risk_net_edge_bps",
            safe_edge, min_safe, slippage_buf, exec_buf, de_risk,
        )


# ══════════════════════════════════════════════════════════════════
#  查询摘要（供 operator / API 使用）
# ══════════════════════════════════════════════════════════════════


def get_active_parameter_summary(
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """获取所有 active parameter sets 的摘要."""
    registry = load_active_parameter_registry(project_root=project_root)
    active_sets = registry.get("active_sets", {})

    summary_items: list[dict[str, Any]] = []
    for combo_key, entry in active_sets.items():
        values = entry.get("values", {})
        summary_items.append({
            "combo_key": combo_key,
            "family": entry.get("family", ""),
            "timeframe": entry.get("timeframe", ""),
            "parameter_set_id": entry.get("parameter_set_id", ""),
            "status": entry.get("status", "active") or "active",
            "applied_at": entry.get("applied_at", ""),
            "applied_by": entry.get("applied_by", ""),
            "approval_recommendation_id": entry.get("approval_recommendation_id"),
            "source_round_id": entry.get("source_round_id"),
            "parameter_count": len(values),
            "values": values,
        })

    active_combo_set = {s["combo_key"] for s in summary_items}
    quarantined_combos = dict(registry.get("quarantined_combos") or {})
    db_load_failed = registry.get("db_load_failed") is True
    governance_managed = bool(registry.get("governance_managed", False))
    audit_only = db_load_failed or bool(quarantined_combos)
    if db_load_failed:
        reason_code = "governance_unavailable"
        runtime_source = "governance_unavailable"
    elif quarantined_combos:
        reason_code = "governance_quarantine"
        runtime_source = "governance_quarantine"
    elif governance_managed:
        reason_code = None
        runtime_source = "active_parameters"
    else:
        reason_code = None
        runtime_source = "profile_defaults"
    return {
        "generated_at": registry.get("generated_at"),
        "available": not db_load_failed,
        "audit_only": audit_only,
        "reason_code": reason_code,
        "runtime_source": runtime_source,
        "db_load_failed": db_load_failed,
        "governance_managed": governance_managed,
        "quarantined_combos": quarantined_combos,
        "paused_combos": sorted(registry.get("paused_combos", [])),
        "total_active_sets": len(summary_items),
        "known_combos": [c["key"] for c in KNOWN_COMBOS],
        "active_combos": sorted(active_combo_set),
        "missing_combos": [
            c["key"] for c in KNOWN_COMBOS
            if c["key"] not in active_combo_set
        ],
        "active_sets": active_sets,
        "parameter_sets": summary_items,
    }
