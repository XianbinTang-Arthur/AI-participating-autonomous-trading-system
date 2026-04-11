"""Replay context: bar-level data model for historical replay.

Phase 2 replay 以 Gold replay bars 为输入，逐 bar 构建上下文供策略 adapter 评估。
本模块定义 replay 流程中所有共享的数据结构。

Edge Contract（P0-3 统一语义）：
    所有 family adapter 必须按以下 5 层分解输出 edge：
    - signal_edge_proxy_bps:   来自策略信号（score / momentum / trend / alpha）的机会代理
    - funding_adjustment_bps:  来自 funding rate 的附加调整
    - cost_bps:               成本总计（blended fee + slippage，费率按执行策略混合 maker/taker）
    - noise_buffer_bps:       信号噪声缓冲（对齐生产端 strategy_edge_noise_buffer_bps）
    - expected_net_edge_bps:  = signal_edge_proxy_bps + funding_adjustment_bps - cost_bps - noise_buffer_bps

    内部估算方式可以不同，但输出语义必须统一。
"""

from __future__ import annotations

import dataclasses as dc
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal


# ---------------------------------------------------------------------------
# 输入：Gold replay bar 行
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayBar:
    """从 gold.market_*_replay_bars_* 读取的一行。"""
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    quote_volume: Decimal | None
    is_closed: bool
    aligned_funding_rate: Decimal | None
    funding_source_ts: datetime | None


# ---------------------------------------------------------------------------
# Replay 成本配置
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayCostConfig:
    """可配置的交易成本模型。

    默认值对齐生产 derivatives_live 配置（maker/taker 混合费率 + 合理滑点）。
    所有值单位为 bps（1 bps = 0.01%）。

    OKX 费率参考（2024/2025）：
    - Swap taker fee:  0.05% = 5 bps（普通用户），VIP 可低至 2-3 bps
    - Swap maker fee:  0.02% = 2 bps（普通用户）
    - 滑点:           OKX BTC-USDT-SWAP 实际 <1 bps（derivatives_live.yaml 注释）

    费率混合公式（完整对齐 fee_resolver.py:175-182）：
    当 execution_style 为 limit 类型时，
    maker_bias  = clamp(-maker_taker_bias, 0, 1)
    maker_weight = clamp(0.15 + passive_bias * 0.45 + maker_bias * 0.20, 0, 0.80)
    blended_fee  = taker * (1 - maker_weight) + maker * maker_weight

    execution_style 名称映射：
    - 生产 fee_resolver 原生识别: bounded_limit_ioc / maker / passive
    - 策略层别名（执行层映射后走同一路径）: passive_first / bounded_limit
    - taker 类: taker / bounded_taker_cap / exchange / market → 不混合
    """
    taker_fee_bps: float = 5.0       # OKX swap taker 0.05% = 5 bps
    slippage_bps: float = 1.0        # 对齐生产 configured_slippage ≈ max_tolerance(20)×fraction(0.05)=1.0
    maker_fee_bps: float = 2.0       # OKX swap maker 0.02% = 2 bps
    execution_style: str = "passive_first"   # 对齐 strategy_hedge_independent_entry_execution_mode
    passive_bias: float = 0.7        # passive_first 默认 0.7，bounded_limit 默认 0.5
    maker_taker_bias: float = 0.0    # 生产默认 0；非零时偏移 maker_weight（fee_resolver.py:177）

    # fee_resolver.py:175 原生识别的 limit 类 style + 策略层别名
    _BLENDED_STYLES: ClassVar[frozenset[str]] = frozenset({
        "bounded_limit_ioc", "maker", "passive",   # fee_resolver 原生
        "passive_first", "bounded_limit",           # 策略层别名
    })

    @property
    def blended_fee_bps(self) -> float:
        """按执行策略计算的混合费率（bps）。

        完整对齐 fee_resolver.py:175-182，包含 maker_taker_bias 项。
        """
        style = self.execution_style.lower()
        if style in self._BLENDED_STYLES:
            passive = min(max(self.passive_bias, 0.0), 1.0)
            maker_bias = min(max(-self.maker_taker_bias, 0.0), 1.0)
            maker_weight = min(max(0.15 + passive * 0.45 + maker_bias * 0.20, 0.0), 0.80)
            return self.taker_fee_bps * (1.0 - maker_weight) + self.maker_fee_bps * maker_weight
        return self.taker_fee_bps

    @property
    def total_cost_bps(self) -> float:
        """单次开平仓的单边成本（bps）= blended_fee + slippage。"""
        return self.blended_fee_bps + self.slippage_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "taker_fee_bps": self.taker_fee_bps,
            "slippage_bps": self.slippage_bps,
            "maker_fee_bps": self.maker_fee_bps,
            "execution_style": self.execution_style,
            "passive_bias": self.passive_bias,
            "maker_taker_bias": self.maker_taker_bias,
            # 以下为只读计算属性（from_dict 不消费，仅供可观测性）
            "blended_fee_bps": self.blended_fee_bps,
            "total_cost_bps": self.total_cost_bps,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplayCostConfig:
        return cls(
            taker_fee_bps=float(d.get("taker_fee_bps", 5.0)),
            slippage_bps=float(d.get("slippage_bps", 1.0)),
            maker_fee_bps=float(d.get("maker_fee_bps", 2.0)),
            execution_style=str(d.get("execution_style", "passive_first")),
            passive_bias=float(d.get("passive_bias", 0.7)),
            maker_taker_bias=float(d.get("maker_taker_bias", 0.0)),
        )


# ---------------------------------------------------------------------------
# Replay 参数覆盖
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayParameterOverrides:
    """可在 replay 实验中覆盖的策略参数。

    Phase 2 冻结参数（均可通过 CLI --param key=value 覆盖）：

    策略门槛参数：
    - min_confirm_ticks           信号确认强度
    - score_stability_threshold   强信号是否被过度拦截
    - min_safe_net_edge_bps       边缘机会放行下限

    Signal edge 校准参数：
    - signal_edge_scale_bps       score -> bps 的缩放系数（影响 signal proxy 绝对值）
    - directional_trend_weight    directional 的趋势/return 混合权重（0~1）
    - directional_return_clamp_bps  directional bar return 限幅（bps）

    Phase 1 扩展参数（与 production settings 直接语义对齐）：
    - entry_threshold             开仓评分阈值（long）
    - close_threshold             平仓评分阈值（long）
    - scale_in_threshold          加仓评分阈值（long）
    - short_entry_threshold       开仓评分阈值（short，None = 同 entry_threshold）
    - short_close_threshold       平仓评分阈值（short，None = 同 close_threshold）
    - min_hold_seconds            最小持仓秒数
    - rebalance_cooldown_seconds  平仓后冷却秒数
    - max_thesis_age_seconds      thesis 最长存活秒数
    - de_risk_net_edge_bps        降风险触发阈值（bps）
    - failed_thesis_net_edge_bps  thesis 失效阈值（bps）
    - expected_slippage_buffer_bps 滑点缓冲（bps）
    - expected_execution_buffer_bps 执行缓冲（bps）
    - max_acceptable_cost_bps     最大允许单边成本（bps）
    - min_score_drawdown_bps      最大评分回撤容忍（bps）
    - min_liquidity_quality       最低流动性质量分
    - limit_offset_bps_entry      限价偏移（bps）

    成本模型（也可通过 --param taker_fee_bps=5 直接覆盖）：
    - cost_config                 交易成本配置（taker_fee_bps + slippage_bps）
    """
    min_confirm_ticks: int = 2
    score_stability_threshold: float = 5.0
    min_safe_net_edge_bps: float = 2.0

    # Signal edge 校准参数（收口在这里，不锁死在 adapter 内部常量）
    signal_edge_scale_bps: float = 12.0
    """score -> bps 的缩放系数。score=0.6 * 12 = 7.2 bps 信号代理。
    与 _PARAM_DEFAULTS (rdp_run_step3_research.py) 对齐，两端唯一真相源。"""

    directional_trend_weight: float = 0.7
    """directional adapter 里 趋势强度 vs bar return 的混合权重。
    signal = weight * trend_signal + (1-weight) * clamped_return。"""

    directional_return_clamp_bps: float = 20.0
    """directional adapter 里 bar return 的限幅（bps）。防止单根极端 bar 主导 signal。"""

    # ── Phase 1 扩展：进出场阈值 ──────────────────────────────────
    # 默认值对齐 derivatives_live.yaml 实盘钉住值
    entry_threshold: float = 0.30
    """long book 开仓评分门槛。生产端映射: strategy_hedge_independent_long_entry_threshold
    对齐 derivatives_live.yaml:342 实盘钉住值 0.30。"""

    close_threshold: float = 0.15
    """long book 平仓评分门槛。生产端映射: strategy_hedge_independent_long_close_threshold"""

    scale_in_threshold: float = 0.40
    """long book 加仓评分门槛。生产端映射: strategy_hedge_independent_long_scale_in_threshold
    对齐 derivatives_live.yaml:353 实盘钉住值 0.40。
    ⚠️ REPLAY 未模拟: 当前 replay 只有 open/hold/close 三态，没有 scale-in（加仓）逻辑。
    该参数仅做透传映射到生产端，replay 回测不验证其效果。"""

    short_entry_threshold: float | None = None
    """short book 开仓阈值。None 时使用 entry_threshold（对称模式）。"""

    short_close_threshold: float | None = None
    """short book 平仓阈值。None 时使用 close_threshold（对称模式）。"""

    # ── Phase 1 扩展：持仓时间管理 ────────────────────────────────
    min_hold_seconds: float = 300.0
    """最小持仓秒数，防止过频交易。生产端映射: strategy_hedge_independent_long_min_hold_seconds"""

    rebalance_cooldown_seconds: float = 120.0
    """平仓后冷却秒数。生产端映射: strategy_hedge_independent_rebalance_cooldown_seconds"""

    max_thesis_age_seconds: float = 1800.0
    """thesis 最长存活秒数。生产端映射: strategy_hedge_independent_max_thesis_age_seconds"""

    # ── Phase 1 扩展：风险管理阈值 ────────────────────────────────
    de_risk_net_edge_bps: float = 2.0
    """净边际变薄时触发降风险的阈值（bps）。生产端映射: strategy_hedge_independent_de_risk_net_edge_bps"""

    failed_thesis_net_edge_bps: float = -1.0
    """净边际低于此值视为 thesis 失效并退出（bps）。
    约束: 必须 <= de_risk_net_edge_bps。
    生产端映射: strategy_hedge_independent_failed_thesis_net_edge_bps"""

    catastrophic_failed_thesis_buffer_bps: float = 3.0
    """灾难性 failed_thesis 缓冲（bps），whipsaw 防护阈值。
    仅当 expected_net_edge_bps <= failed_thesis_net_edge_bps - 此缓冲 时，
    判定为灾难性 thesis 失效，允许豁免 min_hold 立即出场。
    默认 3.0 bps：覆盖 BTC-USDT-SWAP 正常噪声带（~1-2 bps），
    确保只有真实深度亏损才触发紧急止损。
    生产端映射: strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps"""

    # ── Phase 1 扩展：成本缓冲 ────────────────────────────────────
    expected_slippage_buffer_bps: float = 0.5
    """开仓预期滑点缓冲（bps）。叠加到 cost 计算中。
    生产端映射: strategy_hedge_independent_expected_slippage_buffer_bps"""

    expected_execution_buffer_bps: float = 0.5
    """开仓执行缓冲（bps）。叠加到 cost 计算中。
    生产端映射: strategy_hedge_independent_expected_execution_buffer_bps"""

    max_acceptable_cost_bps: float = 7.5
    """最大允许的单边预期成本（bps）。超出则阻断。
    生产端映射: strategy_hedge_independent_max_acceptable_cost_bps"""

    # ── Phase 1 扩展：评分质量 ────────────────────────────────────
    min_score_drawdown_bps: float | None = 6.0
    """评分最大回撤容忍度（bps）。None 时仅使用 score_stability_threshold。
    对齐 derivatives_live.yaml:389 实盘值 6.0。
    生产端映射: strategy_hedge_independent_min_score_drawdown_bps"""

    min_liquidity_quality: float = 0.55
    """最低流动性质量分。replay 默认 liq=1.0，此参数做灵敏度分析。
    生产端映射: strategy_hedge_independent_min_liquidity_quality"""

    # ── Phase 1 扩展：执行策略 ────────────────────────────────────
    limit_offset_bps_entry: float = 1.5
    """开仓限价偏移（bps）。影响成交率与滑点。
    生产端映射: strategy_hedge_independent_limit_offset_bps_entry
    ⚠️ REPLAY 未模拟: replay 假设 bar close 即时成交，不模拟 limit order 匹配。
    该参数仅做透传映射到生产端，replay 回测不验证其效果。"""

    # ── 信号噪声缓冲 ─────────────────────────────────────────
    noise_buffer_bps: float = 2.0
    """信号噪声缓冲（bps），从 net_edge 中扣除。
    对齐生产端 strategy_edge_noise_buffer_bps（derivatives_live.yaml = 2.0）。
    ⚠️ 此参数为 YAML-only，不在 RDP active_parameters 映射中，
    但校准时必须纳入以匹配生产门控行为。"""

    # 成本配置
    cost_config: ReplayCostConfig = dc.field(default_factory=ReplayCostConfig)

    # 可扩展的额外参数
    extra: dict[str, Any] = dc.field(default_factory=dict)

    def __post_init__(self) -> None:
        """参数约束校验（frozen dataclass 只能 raise，不能 mutate）。"""
        if self.failed_thesis_net_edge_bps > self.de_risk_net_edge_bps:
            raise ValueError(
                f"约束违反: failed_thesis_net_edge_bps ({self.failed_thesis_net_edge_bps}) "
                f"必须 <= de_risk_net_edge_bps ({self.de_risk_net_edge_bps})"
            )
        if self.catastrophic_failed_thesis_buffer_bps < 0.0:
            raise ValueError(
                f"约束违反: catastrophic_failed_thesis_buffer_bps "
                f"({self.catastrophic_failed_thesis_buffer_bps}) 必须 >= 0"
            )
        if self.close_threshold > self.entry_threshold:
            raise ValueError(
                f"约束违反: close_threshold ({self.close_threshold}) "
                f"应当 <= entry_threshold ({self.entry_threshold})"
            )
        if self.scale_in_threshold < self.entry_threshold:
            raise ValueError(
                f"约束违反: scale_in_threshold ({self.scale_in_threshold}) "
                f"应当 >= entry_threshold ({self.entry_threshold})"
            )
        if self.short_entry_threshold is not None and self.short_close_threshold is not None:
            if self.short_close_threshold > self.short_entry_threshold:
                raise ValueError(
                    f"约束违反: short_close_threshold ({self.short_close_threshold}) "
                    f"应当 <= short_entry_threshold ({self.short_entry_threshold})"
                )
        safe_edge = (
            self.min_safe_net_edge_bps
            + self.expected_slippage_buffer_bps
            + self.expected_execution_buffer_bps
        )
        # 要求 safe_edge >= de_risk + 1.0 bps 最小间距
        # 目的: 持仓区间 [safe_edge, ∞) 与 de_risk 区间 (-∞, de_risk] 之间
        # 至少有 1 bps hysteresis 带，避免边际信号反复翻转 entry/de_risk
        if safe_edge < self.de_risk_net_edge_bps + 1.0:
            raise ValueError(
                f"约束违反: safe_edge ({self.min_safe_net_edge_bps} + "
                f"{self.expected_slippage_buffer_bps} + "
                f"{self.expected_execution_buffer_bps} = {safe_edge}) "
                f"必须 >= de_risk_net_edge_bps ({self.de_risk_net_edge_bps}) + 1.0 bps，"
                f"否则持仓 hysteresis 带过窄会导致边际信号反复翻转"
            )
        if self.min_hold_seconds > self.max_thesis_age_seconds:
            raise ValueError(
                f"约束违反: min_hold_seconds ({self.min_hold_seconds}) "
                f"必须 <= max_thesis_age_seconds ({self.max_thesis_age_seconds})，"
                f"否则 min_hold 锁定期间 stale_thesis 无法触发正常退出"
            )

    # ── 工厂方法：按 family 获取合理默认参数 ─────────────────────
    @classmethod
    def for_family(cls, family: str = "independent") -> "ReplayParameterOverrides":
        """获取指定 family 的默认参数。

        directional 家族原始硬编码阈值与 independent 不同：
          - directional: entry=0.45, close=0.20
          - independent: entry=0.40, close=0.15
        使用本方法可避免共享默认值导致的静默行为变更。
        """
        if family == "directional":
            return cls(
                entry_threshold=0.45,
                close_threshold=0.20,
            )
        return cls()

    # ── 辅助方法：获取方向特定阈值 ──────────────────────────────
    def get_entry_threshold(self, leg: str = "long") -> float:
        """获取指定方向的开仓阈值。"""
        if leg == "short" and self.short_entry_threshold is not None:
            return self.short_entry_threshold
        return self.entry_threshold

    def get_close_threshold(self, leg: str = "long") -> float:
        """获取指定方向的平仓阈值。"""
        if leg == "short" and self.short_close_threshold is not None:
            return self.short_close_threshold
        return self.close_threshold

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "min_confirm_ticks": self.min_confirm_ticks,
            "score_stability_threshold": self.score_stability_threshold,
            "min_safe_net_edge_bps": self.min_safe_net_edge_bps,
            "signal_edge_scale_bps": self.signal_edge_scale_bps,
            "directional_trend_weight": self.directional_trend_weight,
            "directional_return_clamp_bps": self.directional_return_clamp_bps,
            # Phase 1 扩展
            "entry_threshold": self.entry_threshold,
            "close_threshold": self.close_threshold,
            "scale_in_threshold": self.scale_in_threshold,
            "short_entry_threshold": self.short_entry_threshold,
            "short_close_threshold": self.short_close_threshold,
            "min_hold_seconds": self.min_hold_seconds,
            "rebalance_cooldown_seconds": self.rebalance_cooldown_seconds,
            "max_thesis_age_seconds": self.max_thesis_age_seconds,
            "de_risk_net_edge_bps": self.de_risk_net_edge_bps,
            "failed_thesis_net_edge_bps": self.failed_thesis_net_edge_bps,
            "catastrophic_failed_thesis_buffer_bps": self.catastrophic_failed_thesis_buffer_bps,
            "expected_slippage_buffer_bps": self.expected_slippage_buffer_bps,
            "expected_execution_buffer_bps": self.expected_execution_buffer_bps,
            "max_acceptable_cost_bps": self.max_acceptable_cost_bps,
            "min_score_drawdown_bps": self.min_score_drawdown_bps,
            "min_liquidity_quality": self.min_liquidity_quality,
            "limit_offset_bps_entry": self.limit_offset_bps_entry,
            "noise_buffer_bps": self.noise_buffer_bps,
            "cost_config": self.cost_config.to_dict(),
        }
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplayParameterOverrides:
        """从字典反序列化。

        支持两种成本传入方式：
        1. 嵌套: {"cost_config": {"taker_fee_bps": 5, "slippage_bps": 2}}
        2. 平铺（CLI 友好）: {"taker_fee_bps": 5, "slippage_bps": 2}
        平铺方式优先级更高（直接来自 --param）。
        """
        known = {
            "min_confirm_ticks", "score_stability_threshold",
            "min_safe_net_edge_bps", "signal_edge_scale_bps",
            "directional_trend_weight", "directional_return_clamp_bps",
            "cost_config",
            # 平铺 cost keys（from_dict 时消费，不进 extra）
            "taker_fee_bps", "slippage_bps",
            "maker_fee_bps", "execution_style", "passive_bias", "maker_taker_bias",
            # Phase 1 扩展参数
            "entry_threshold", "close_threshold", "scale_in_threshold",
            "short_entry_threshold", "short_close_threshold",
            "min_hold_seconds", "rebalance_cooldown_seconds",
            "max_thesis_age_seconds",
            "de_risk_net_edge_bps", "failed_thesis_net_edge_bps",
            "catastrophic_failed_thesis_buffer_bps",
            "expected_slippage_buffer_bps", "expected_execution_buffer_bps",
            "max_acceptable_cost_bps",
            "min_score_drawdown_bps", "min_liquidity_quality",
            "limit_offset_bps_entry",
            "noise_buffer_bps",
        }

        # null-safe 取值：JSON null → 用默认值
        def _v(key: str, default: float) -> float:
            val = d.get(key)
            return float(val) if val is not None else float(default)

        # null-safe 可选值
        def _v_opt(key: str) -> float | None:
            val = d.get(key)
            return float(val) if val is not None else None

        # 成本配置：优先从平铺 keys 组装，其次从嵌套 cost_config
        has_flat_cost = "taker_fee_bps" in d or "slippage_bps" in d or "maker_fee_bps" in d
        if has_flat_cost:
            cost = ReplayCostConfig(
                taker_fee_bps=_v("taker_fee_bps", 5.0),
                slippage_bps=_v("slippage_bps", 2.0),
                maker_fee_bps=_v("maker_fee_bps", 2.0),
                execution_style=str(d.get("execution_style", "passive_first")),
                passive_bias=_v("passive_bias", 0.7),
                maker_taker_bias=_v("maker_taker_bias", 0.0),
            )
        else:
            cost_raw = d.get("cost_config")
            cost = ReplayCostConfig.from_dict(cost_raw) if isinstance(cost_raw, dict) else ReplayCostConfig()

        confirm_raw = d.get("min_confirm_ticks")
        confirm = int(confirm_raw) if confirm_raw is not None else 2

        return cls(
            min_confirm_ticks=confirm,
            score_stability_threshold=_v("score_stability_threshold", 5.0),
            min_safe_net_edge_bps=_v("min_safe_net_edge_bps", 2.0),
            signal_edge_scale_bps=_v("signal_edge_scale_bps", 12.0),
            directional_trend_weight=_v("directional_trend_weight", 0.7),
            directional_return_clamp_bps=_v("directional_return_clamp_bps", 20.0),
            # Phase 1 扩展
            entry_threshold=_v("entry_threshold", 0.30),
            close_threshold=_v("close_threshold", 0.15),
            scale_in_threshold=_v("scale_in_threshold", 0.40),
            short_entry_threshold=_v_opt("short_entry_threshold"),
            short_close_threshold=_v_opt("short_close_threshold"),
            min_hold_seconds=_v("min_hold_seconds", 300.0),
            rebalance_cooldown_seconds=_v("rebalance_cooldown_seconds", 120.0),
            max_thesis_age_seconds=_v("max_thesis_age_seconds", 1800.0),
            de_risk_net_edge_bps=_v("de_risk_net_edge_bps", 2.0),
            failed_thesis_net_edge_bps=_v("failed_thesis_net_edge_bps", -1.0),
            catastrophic_failed_thesis_buffer_bps=_v("catastrophic_failed_thesis_buffer_bps", 3.0),
            expected_slippage_buffer_bps=_v("expected_slippage_buffer_bps", 0.5),
            expected_execution_buffer_bps=_v("expected_execution_buffer_bps", 0.5),
            max_acceptable_cost_bps=_v("max_acceptable_cost_bps", 7.5),
            # min_score_drawdown_bps: key 缺失→6.0（新默认）; 显式 null→None（禁用 drawdown 检查）
            min_score_drawdown_bps=(
                float(d["min_score_drawdown_bps"]) if d.get("min_score_drawdown_bps") is not None
                else (None if "min_score_drawdown_bps" in d else 6.0)
            ),
            min_liquidity_quality=_v("min_liquidity_quality", 0.55),
            limit_offset_bps_entry=_v("limit_offset_bps_entry", 1.5),
            noise_buffer_bps=_v("noise_buffer_bps", 2.0),
            cost_config=cost,
            extra={k: v for k, v in d.items() if k not in known},
        )


# ---------------------------------------------------------------------------
# Replay 上下文：传递给策略 adapter 的逐 bar 状态
# ---------------------------------------------------------------------------

@dc.dataclass
class ReplayState:
    """在 replay 过程中跨 bar 累积的可变状态。

    字段生命周期：
      - position_qty/side/entry_price/entry_ts: 由 adapter._advance_state 维护
        不变量: position_side=="flat" 时 entry_price 和 entry_ts 必须为 None
      - last_close_ts: 由 adapter._advance_state 在 close 时设置
      - score_history: 由 replay_runner 外部 append（adapter 内部用独立 deque）
      - bar_index: 由 replay_runner 外部设置（adapter 不读写）
    """
    position_qty: Decimal = Decimal("0")        # 当前持仓
    position_side: Literal["flat", "long", "short"] = "flat"
    entry_price: Decimal | None = None
    entry_ts: datetime | None = None
    score_history: list[float] = dc.field(default_factory=list)   # runner 维护，adapter 不使用
    bar_index: int = 0                                            # runner 维护，adapter 不使用
    last_close_ts: datetime | None = None       # 上次平仓时间（冷却用）


@dc.dataclass(frozen=True)
class ReplayBarContext:
    """单根 bar 传递给 adapter 的完整上下文。"""
    bar: ReplayBar
    bar_index: int
    state: ReplayState
    params: ReplayParameterOverrides
    family: str
    symbol: str
    timeframe: str
    dataset_version: str


# ---------------------------------------------------------------------------
# 输出：逐 bar 决策记录
# ---------------------------------------------------------------------------

@dc.dataclass(frozen=True)
class ReplayDecision:
    """策略 adapter 对单根 bar 的评估结果。

    字段对齐 Phase 2 设计决策文档 §8.3 + P0-3 统一 edge contract。

    Edge 分解（所有 family 统一语义）：
    - signal_edge_proxy_bps:   策略信号派生的机会代理值
    - funding_adjustment_bps:  funding rate 附加调整
    - cost_bps:               交易成本（blended fee + slippage）
    - noise_buffer_bps:       信号噪声缓冲
    - expected_net_edge_bps:  = signal + funding - cost - noise_buffer（最终净 edge）
    """
    ts: datetime
    family: str
    symbol: str
    timeframe: str
    state: str                              # flat / probing / holding / ...
    selectable: bool                        # 是否可选中
    execution_compatible: bool              # 是否可执行
    long_score: float
    short_score: float
    blocking_reasons: list[str]
    expected_net_edge_bps: float
    target_position_qty: Decimal
    delta_position_qty: Decimal

    # Edge 分解字段（P0-3 统一 contract）
    signal_edge_proxy_bps: float = 0.0      # 来自策略信号的机会代理
    funding_adjustment_bps: float = 0.0     # 来自 funding rate 的附加调整
    cost_bps: float = 0.0                   # 交易成本
    noise_buffer_bps: float = 0.0           # 信号噪声缓冲

    # 扩展字段
    action: str = "hold"                    # open / hold / close / blocked
    close_reason: str = ""                  # thesis_failed / de_risk / score_below_close / direction_reversal / thesis_stale
    score_stable: bool = False
    funding_rate: float | None = None
    close_price: float | None = None
    bar_index: int = 0

    def to_flat_dict(self) -> dict[str, Any]:
        """序列化为平坦字典（写 CSV / parquet 用）。"""
        return {
            "ts": self.ts.isoformat(),
            "family": self.family,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state,
            "selectable": self.selectable,
            "execution_compatible": self.execution_compatible,
            "long_score": self.long_score,
            "short_score": self.short_score,
            "blocking_reasons": "|".join(self.blocking_reasons) if self.blocking_reasons else "",
            "signal_edge_proxy_bps": self.signal_edge_proxy_bps,
            "funding_adjustment_bps": self.funding_adjustment_bps,
            "cost_bps": self.cost_bps,
            "noise_buffer_bps": self.noise_buffer_bps,
            "expected_net_edge_bps": self.expected_net_edge_bps,
            "target_position_qty": str(self.target_position_qty),
            "delta_position_qty": str(self.delta_position_qty),
            "action": self.action,
            "close_reason": self.close_reason,
            "score_stable": self.score_stable,
            "funding_rate": self.funding_rate,
            "close_price": self.close_price,
            "bar_index": self.bar_index,
        }
