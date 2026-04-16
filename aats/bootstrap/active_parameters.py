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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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
    # ⚠️ REPLAY 未模拟: replay 假设 bar close 即时成交，无 limit order 匹配模型
    #    该参数仅透传到生产端，RDP 回测不验证其效果
    "limit_offset_bps_entry": "strategy_hedge_independent_limit_offset_bps_entry",
}

PARAMETER_MAPPING_DIRECTIONAL: dict[str, str] = {
    # [PLACEHOLDER] RDP 方向性策略的趋势权重 → 生产端 entry alpha 最小值
    # RDP 端: directional_trend_weight 是趋势信号在综合评分中的权重 (0~1)
    # 生产端: strategy_entry_alpha_min 是入场信号的最小 alpha 阈值
    # ⚠️ 语义张力较大: "权重" ≠ "最小阈值"
    #    第一版占位: 假设 trend_weight 越高 → 要求的 alpha_min 越高
    #    TODO: 需要明确两者的数学关系，或拆成独立映射
    "directional_trend_weight": "strategy_entry_alpha_min",

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

# ── RDP 研究参数的 "规范 key 集合"，用于检测映射缺失 ──────────────
#
# 这些 key 是 RDP Step 3 研究层输出的核心参数名，理论上每个家族都应
# 提供完整映射。当 build_settings_overrides 发现 JSON 中存在这些 key
# 但 family 映射缺失时，会记录 WARNING 级日志并统计被丢弃的参数数，
# 以帮助快速发现 "研究输出了参数但生产端没接上" 的映射漏洞。
#
# 非此集合中的参数（如 cost_config 子字段）不会触发警告。
_RDP_CORE_RESEARCH_PARAMS: frozenset[str] = frozenset({
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
})

# ── 参数白名单: 即使映射缺失，这些 key 也不会触发 WARNING ─────────
#
# 某些 key 不属于 "RDP 层需要注入生产端" 的范畴，例如:
#   - cost_config / taker_fee_bps / slippage_bps: 仅供 replay 成本模型
#   - directional_trend_weight / directional_return_clamp_bps: 仅供 replay adapter
#
# 这些参数由 ReplayParameterOverrides 消费，无需透传到 AATSSettings。
_RDP_REPLAY_ONLY_PARAMS: frozenset[str] = frozenset({
    "cost_config",
    "taker_fee_bps",
    "slippage_bps",
    "directional_trend_weight",
    "directional_return_clamp_bps",
})

# ── 默认路径（兼容常量，外部调用方仍引用） ─────────────────────────

DEFAULT_ACTIVE_DIR = "configs/active_parameter_sets"
DEFAULT_REGISTRY_FILENAME = "active_parameter_registry.json"


# ══════════════════════════════════════════════════════════════════
#  DB 数据源
# ══════════════════════════════════════════════════════════════════


def _try_load_from_db(db_url: str | None = None) -> dict[str, Any] | None:
    """尝试从数据库加载 active parameter registry.

    同时查询 ``governance.active_decisions`` 表：
    - 如果治理层已接管（表存在且有行），返回结果中
      ``governance_managed=True`` + ``paused_combos=[...]``。
    - 调用方据此决定是否 fallback 到文件 registry。
      治理层主动 pause �� combo 不应从文件补齐。

    Returns
    -------
    dict | None  成功时返回 registry dict，失败或不可用时返回 None。
    """
    # 确定 DB URL: 显式传入 > 环境变量 > None
    url = db_url or os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if not url:
        return None

    try:
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        try:
            with engine.connect() as conn:
                rows = conn.execute(sa_text(
                    "SELECT family, timeframe, parameter_set_id, "
                    "values AS param_values, "
                    "source_round_id, approval_recommendation_id, applied_by, applied_at "
                    "FROM governance.active_parameter_sets ORDER BY family, timeframe"
                )).fetchall()

                # ── 查询治理决策状态 ──
                # 如果 governance.active_decisions 存在且有行，说明治理层
                # 已接管参数管理。DB 返回空 active sets 是治���层主动 pause
                # 的结果，不应 fallback 到文件 registry。
                governance_managed = False
                paused_combos: set[str] = set()
                try:
                    decision_rows = conn.execute(sa_text(
                        "SELECT combo_key, current_status "
                        "FROM governance.active_decisions"
                    )).fetchall()
                    if decision_rows:
                        governance_managed = True
                        for dr in decision_rows:
                            if str(dr.current_status).lower() == "pause":
                                paused_combos.add(dr.combo_key)
                except Exception:
                    # governance 表尚未创建（首次部署等），忽略
                    pass
        finally:
            engine.dispose()

        active_sets: dict[str, Any] = {}
        skipped_paused: list[str] = []
        for row in rows:
            combo_key = f"{row.family}_{row.timeframe}"
            if combo_key in paused_combos:
                skipped_paused.append(combo_key)
                continue
            active_sets[combo_key] = {
                "parameter_set_id": row.parameter_set_id,
                "family": row.family,
                "timeframe": row.timeframe,
                "values": row.param_values,
            }

        if skipped_paused:
            log.info(
                "active_parameter_governance_paused: 跳过 %d 个被治理层暂停的 combo: %s",
                len(skipped_paused), ", ".join(sorted(skipped_paused)),
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
        }

    except Exception as exc:
        # DB URL 已配置但连接/查询失败 —— 这是生产环境的实质性降级：
        # 系统将回退到 profile 默认参数而非用户调优后的 active set。
        # 提升日志级别到 error 确保运维能在日志聚合中立即看到。
        log.error(
            "active_parameter_db_load_failed: 数据库加载 active parameters 失败，"
            "系统将退化为 profile 默认参数。DB URL 已配置但不可达。err=%s",
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
    DB 不可用时 fail-soft 返回空 registry，不中断主系统。

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

    # DB 不可用时 fail-soft，但需区分 "没配置 DB"（开发/测试）和
    # "配置了 DB 但连接失败"（生产实质性降级）。
    effective_url = db_url or os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if effective_url:
        log.error(
            "active_parameter_registry_degraded: DB URL 已配置但加载失败，"
            "返回空 registry。策略将运行在 profile 默认参数上，可能与期望不符。",
        )
        return {
            "generated_at": None,
            "active_sets": {},
            "db_load_failed": True,
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

    # 治理层是否已接管参数管理（DB 查询时同步获取）
    governance_managed = registry.get("governance_managed", False)

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
    applied_combos: list[str] = []
    # combo_key -> 被丢弃的参数集合（用于诊断映射缺失）
    dropped_by_combo: dict[str, list[str]] = {}

    for combo_key, data in all_sets.items():
        parts = combo_key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        family, timeframe = parts

        if families and family not in families:
            continue
        if timeframes and timeframe.lower() not in [t.lower() for t in timeframes]:
            continue

        mapping = FAMILY_PARAMETER_MAPPINGS.get(family, {})
        values = data.get("values", {})

        dropped: list[str] = []
        for rdp_param in values.keys():
            if rdp_param in mapping:
                continue  # 已被映射
            if rdp_param in _RDP_REPLAY_ONLY_PARAMS:
                continue  # 属于 replay-only, 不应映射
            if rdp_param in _RDP_CORE_RESEARCH_PARAMS:
                dropped.append(rdp_param)
        if dropped:
            dropped_by_combo[combo_key] = sorted(dropped)
            log.error(
                "Active parameter combo skipped [%s]: core research params are not "
                "fully mapped to AATSSettings. Missing keys: %s",
                combo_key,
                ", ".join(sorted(dropped)),
            )
            continue

        for rdp_param, settings_field in mapping.items():
            if rdp_param not in values:
                continue
            if values[rdp_param] is None:
                log.warning(
                    "Active parameter skipped None value [%s]: %s",
                    combo_key,
                    rdp_param,
                )
                continue
            overrides[settings_field] = values[rdp_param]

        applied_combos.append(combo_key)

    if applied_combos:
        log.info(
            "Active parameter overrides: %d fields from %s",
            len(overrides),
            ", ".join(applied_combos),
        )

    # 映射缺失 WARNING: 帮助运维/开发者快速定位 "研究输出 → 生产注入" 断链
    for combo_key, dropped in dropped_by_combo.items():
        family, _, _timeframe = combo_key.partition("_")
        log.warning(
            "Active parameter mapping gap [%s]: %d research params present in JSON "
            "but not mapped to AATSSettings (family=%s). Dropped keys: %s. "
            "Check FAMILY_PARAMETER_MAPPINGS['%s'] to add missing entries.",
            combo_key,
            len(dropped),
            family,
            ", ".join(dropped),
            family,
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
    return {
        "generated_at": registry.get("generated_at"),
        "governance_managed": bool(registry.get("governance_managed", False)),
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
