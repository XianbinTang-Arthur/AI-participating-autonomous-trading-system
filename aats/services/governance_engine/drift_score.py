"""Stage 9 drift score — 把 4 类 10 个指标压缩成一个 0-8 的整数。

设计文档
========
docs/task/stage_9_abort_hooks_design.md §4

核心不变量
==========
I1 ``compute_drift_score(DriftInputs(...))`` 是**纯函数**，不读文件、不访问
   网络、不访问全局状态。所有依赖通过 DriftInputs 传入。
I2 任一指标为 ``None`` 都归一化为 0（不贡献分数），并在 ``notes`` 里加上
   "missing data" 提示。这样数据源偶发性不可用不会产生假警报。
I3 每个子类（financial / execution / decision / data）的 normalized 值在
   ``[0, 2]`` 区间，total_score 严格落在 ``[0, 8]`` 整数区间。
I4 ``state`` 与 ``allow_ladder_upgrade`` / ``abort_hook_action`` 的映射见
   §4.4 / §5.2 的表，这里是单一真相来源。

这个模块只负责**把数字转成结论**。"怎么收集数字"（live 实时 vs 离线 artifact）
交给 ``drift_inputs.py``；"结论出来后怎么 halt 系统"交给 ``abort_hooks.py``。
三层解耦是为了让 unit test 能用纯 in-memory dict 驱动，不必起任何 sidecar。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

# ─────────────────────────────────────────────────────────────────────
# Schema 版本 & 常量
# ─────────────────────────────────────────────────────────────────────

SCHEMA_VERSION = "stage9.drift_score/v1"
"""DriftReport JSON schema 版本。兼容性约束：同一 major 版本内只加新字段
不改现有字段的语义。上游 consumer（checklist-4 abort hook / grafana json
source / CI gate 脚本）靠这个字符串判断。"""

# 各子类在总分里的权重。放大 4 倍之后 total ∈ [0, 8]（见设计文档 §4.3）
_WEIGHT_FINANCIAL = 1 / 3
_WEIGHT_EXECUTION = 1 / 4
_WEIGHT_DECISION = 1 / 4
_WEIGHT_DATA = 1 / 6
_WEIGHT_SCALE = 4  # 把加权和从 [0, 2] 放大到 [0, 8]
_TOTAL_MAX = 8
_TOTAL_MIN = 0


# ─────────────────────────────────────────────────────────────────────
# Stage 枚举
# ─────────────────────────────────────────────────────────────────────

StageTier = Literal["T0", "T1", "T2", "T3", "T4"]

# 每个阶梯的名义规模（USDT）。与 docs/task/stage_9_dryrun_checklist.md §0 对齐。
# T0 用 1 USDT 作为分母的 sentinel，避免除以零 —— T0 DRY 阶段所有 ratio
# 都应该是 0，所以 nominal 用多少都无所谓。
STAGE_NOMINAL_USDT: dict[StageTier, Decimal] = {
    "T0": Decimal("1"),
    "T1": Decimal("1"),
    "T2": Decimal("10"),
    "T3": Decimal("100"),
    "T4": Decimal("1000"),
}


# ─────────────────────────────────────────────────────────────────────
# 输入结构
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriftInputs:
    """``compute_drift_score`` 的唯一入参。

    所有字段都允许 ``None``，归一化时按"missing data → 0 分"处理。
    stage 必填，window_hours 必填——没有 stage 就谈不上"名义规模"，没有
    window 就谈不上"滚动统计"。

    指标含义见 docs/task/stage_9_abort_hooks_design.md §4.2。
    """

    stage: StageTier
    window_hours: int
    evaluated_at: datetime

    # ── 财务类 ─────────────────────────────────────────
    balance_drift_ratio: Decimal | None = None
    """abs(实际余额变化 - 期望 pnl) / nominal_scale_usdt。
    T0 DRY 没有真账户，所有 paper fill 都会让这个值为 0。"""

    max_drawdown_ratio: Decimal | None = None
    """(窗口峰值余额 - 当前余额) / nominal_scale_usdt，至少 0。"""

    fee_to_pnl_ratio: Decimal | None = None
    """24h 累计手续费 / max(abs(24h realized pnl), nominal_scale_usdt × 1%)。
    除数的下界保证"0 pnl 0 fee"不会得出 NaN。"""

    # ── 执行类 ─────────────────────────────────────────
    fill_success_ratio: Decimal | None = None
    """fill_events 数 / order_intents 数，滚动 1h。"""

    adverse_slippage_ratio: Decimal | None = None
    """高滑点 fill 数 / 总 fill 数，滚动 1h。trial_guard 同款定义。"""

    # ── 决策类 ─────────────────────────────────────────
    decision_cycle_cadence_ratio: Decimal | None = None
    """实际完成 decision_cycle 数 / 期望数（窗口内，按 profile.cycle_interval）。
    1.0 = 完全按计划跑，< 0.95 说明 cycle 经常超时或被 halt 阻断。"""

    decision_error_ratio: Decimal | None = None
    """``decision_cycle_error`` 日志数 / 总 ``decision_cycle_*`` 日志数。"""

    # ── 数据链路类 ─────────────────────────────────────
    reconciliation_mismatch_count: int | None = None
    """最近 window_hours 的 ``reconciliation_mismatch`` 事件计数。"""

    nats_handler_error_ratio: Decimal | None = None
    """所有 handler_error 数 / 总 NATS 消息数，窗口内。"""

    okx_rate_limit_count: int | None = None
    """最近 1h 的 ``okx_rest_rate_limited`` 日志次数。"""

    # ── metadata ─────────────────────────────────────
    notes: list[str] = field(default_factory=list)
    """外部调用方可以传一些额外 context 进来（例如"T1 首次跑"）。这些 note
    会原样透传到 DriftReport.notes 的末尾。"""

    def __post_init__(self) -> None:
        if self.stage not in STAGE_NOMINAL_USDT:
            raise ValueError(f"unknown stage tier: {self.stage!r}")
        if self.window_hours <= 0:
            raise ValueError(f"window_hours must be > 0, got {self.window_hours}")


# ─────────────────────────────────────────────────────────────────────
# 输出结构
# ─────────────────────────────────────────────────────────────────────


DriftState = Literal[
    "clean",
    "minor_drift",
    "noticeable_drift",
    "significant_drift",
    "severe_drift",
    "critical_drift",
]

AbortHookAction = Literal[
    "none",           # score ≤ 2
    "warning",        # score 3 or 4，单次命中只记 warning
    "halt_on_repeat", # score 3 or 4，sidecar 应当看有没有连续 2 次
    "halt_immediate", # score ≥ 5，sidecar 应立即 halt
]


@dataclass
class IndicatorReport:
    """单个指标的归一化结果。"""

    name: str
    raw: Decimal | int | None
    normalized: int  # 0 / 1 / 2
    missing: bool = False


@dataclass
class SubscoreReport:
    """一个类别（financial / execution / ...）的聚合。"""

    category: str
    indicators: list[IndicatorReport]
    value: float  # [0, 2] 区间的 mean


@dataclass
class DriftReport:
    """`compute_drift_score` 的完整输出。

    可以用 ``to_dict()`` 序列化成 JSON（用于 DriftReport schema v1 的 wire
    format，见设计文档 §4.5）。
    """

    schema_version: str
    evaluated_at: datetime
    stage: StageTier
    nominal_scale_usdt: Decimal
    window_hours: int

    subscores: dict[str, SubscoreReport]
    total_score: int
    state: DriftState
    allow_ladder_upgrade: bool
    abort_hook_action: AbortHookAction
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "stage": self.stage,
            "nominal_scale_usdt": str(self.nominal_scale_usdt),
            "window_hours": self.window_hours,
            "subscores": {
                name: {
                    "category": sub.category,
                    "value": round(sub.value, 4),
                    "indicators": [
                        {
                            "name": ind.name,
                            "raw": _serialize_raw(ind.raw),
                            "normalized": ind.normalized,
                            "missing": ind.missing,
                        }
                        for ind in sub.indicators
                    ],
                }
                for name, sub in self.subscores.items()
            },
            "total_score": self.total_score,
            "state": self.state,
            "allow_ladder_upgrade": self.allow_ladder_upgrade,
            "abort_hook_action": self.abort_hook_action,
            "notes": list(self.notes),
        }


def _serialize_raw(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return value


# ─────────────────────────────────────────────────────────────────────
# 归一化阈值（指标 → 0/1/2 的映射表）
# ─────────────────────────────────────────────────────────────────────
#
# 这些阈值直接对应设计文档 §4.2 的三张表。集中在这里便于 checklist-5 里
# 根据 T0 DRY 真跑数据微调。每条是 (warning 上限, critical 上限)：
# value ≤ warning → 0 / value ≤ critical → 1 / value > critical → 2

_THRESHOLDS: dict[str, tuple[Decimal, Decimal]] = {
    # financial
    "balance_drift_ratio":           (Decimal("0.01"), Decimal("0.05")),
    "max_drawdown_ratio":            (Decimal("0.03"), Decimal("0.05")),
    "fee_to_pnl_ratio":              (Decimal("0.30"), Decimal("0.60")),
    # execution（注意 fill_success_ratio 方向反了，higher is better）
    "fill_success_ratio":            (Decimal("0.98"), Decimal("0.90")),  # 反向
    "adverse_slippage_ratio":        (Decimal("0.02"), Decimal("0.10")),
    # decision（cadence 也是反向）
    "decision_cycle_cadence_ratio":  (Decimal("0.95"), Decimal("0.80")),  # 反向
    "decision_error_ratio":          (Decimal("0.01"), Decimal("0.05")),
    # data link
    "reconciliation_mismatch_count": (Decimal("0"),    Decimal("2")),
    "nats_handler_error_ratio":      (Decimal("0.001"),Decimal("0.01")),
    "okx_rate_limit_count":          (Decimal("0"),    Decimal("3")),
}

# 标记哪些指标是"越小越好"（默认）vs "越大越好"（反向归一化）
_REVERSE_DIRECTION = frozenset({
    "fill_success_ratio",
    "decision_cycle_cadence_ratio",
})

_CATEGORY_MEMBERS: dict[str, tuple[str, ...]] = {
    "financial": (
        "balance_drift_ratio",
        "max_drawdown_ratio",
        "fee_to_pnl_ratio",
    ),
    "execution": (
        "fill_success_ratio",
        "adverse_slippage_ratio",
    ),
    "decision": (
        "decision_cycle_cadence_ratio",
        "decision_error_ratio",
    ),
    "data": (
        "reconciliation_mismatch_count",
        "nats_handler_error_ratio",
        "okx_rate_limit_count",
    ),
}

_CATEGORY_WEIGHTS: dict[str, float] = {
    "financial": _WEIGHT_FINANCIAL,
    "execution": _WEIGHT_EXECUTION,
    "decision": _WEIGHT_DECISION,
    "data": _WEIGHT_DATA,
}


def _normalize(name: str, raw: Decimal | int | None) -> tuple[int, bool]:
    """把单个 raw 值归一化到 ``{0, 1, 2}``。返回 ``(normalized, missing)``。

    - ``raw is None`` → ``(0, True)``：缺数据按 0 算但标记 missing
    - 正向指标（越小越好）：≤ warning → 0 / ≤ critical → 1 / 否则 2
    - 反向指标（越大越好）：≥ warning → 0 / ≥ critical → 1 / 否则 2
    """
    if raw is None:
        return 0, True
    value = Decimal(raw) if not isinstance(raw, Decimal) else raw
    warning, critical = _THRESHOLDS[name]
    if name in _REVERSE_DIRECTION:
        if value >= warning:
            return 0, False
        if value >= critical:
            return 1, False
        return 2, False
    # 正向
    if value <= warning:
        return 0, False
    if value <= critical:
        return 1, False
    return 2, False


def _build_subscore(category: str, inputs: DriftInputs) -> SubscoreReport:
    members = _CATEGORY_MEMBERS[category]
    indicators: list[IndicatorReport] = []
    for name in members:
        raw = getattr(inputs, name)
        normalized, missing = _normalize(name, raw)
        indicators.append(
            IndicatorReport(
                name=name,
                raw=raw,
                normalized=normalized,
                missing=missing,
            )
        )
    mean_value = sum(i.normalized for i in indicators) / len(indicators)
    return SubscoreReport(category=category, indicators=indicators, value=float(mean_value))


def _state_from_total(total: int) -> DriftState:
    if total <= 0:
        return "clean"
    if total == 1:
        return "minor_drift"
    if total == 2:
        return "noticeable_drift"
    if total == 3:
        return "significant_drift"
    if total == 4:
        return "severe_drift"
    return "critical_drift"


def _abort_action_from(
    total: int,
    subscores: dict[str, SubscoreReport],
) -> AbortHookAction:
    """根据 total_score + subscore 决定 abort hook 应该做什么。

    规则（与设计文档 §4.4 / §5.2 对齐）：
    - total ≥ 5 → halt_immediate
    - 任一 subscore 的 **均值达到 2.0**（即这一类全部 critical）→ halt_immediate
      （对应 halt reason: subscore_<category>_2）
    - total ∈ [3, 4] → halt_on_repeat（sidecar 要看连续两次）
    - total == 2 → warning（禁止升阶梯，但不 halt）
    - total ≤ 1 → none
    """
    if total >= 5:
        return "halt_immediate"
    for sub in subscores.values():
        if sub.value >= 2.0 - 1e-9:
            return "halt_immediate"
    if total >= 3:
        return "halt_on_repeat"
    if total == 2:
        return "warning"
    return "none"


def _allow_upgrade_from(total: int, has_missing: bool) -> bool:
    """checklist-1 §4.4 规定 score ≤ 1 才允许升阶梯。

    额外规则：如果任何指标是 missing data（数据源不可用），**禁止**升阶梯。
    一个指标缺数据本身就是个危险信号，哪怕总分是 0 也不能升。
    """
    if has_missing:
        return False
    return total <= 1


# ─────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────


def compute_drift_score(inputs: DriftInputs) -> DriftReport:
    """把 DriftInputs 压缩成 DriftReport。

    这是本模块唯一的公开入口。设计目标：
    - 纯函数（无 IO / 无全局状态）
    - 对缺数据健壮（None → 0 分 + missing 标记，不抛）
    - 返回结构稳定（所有字段一定存在，便于 consumer 直接 json.dumps）

    调用方（CLI / abort hook sidecar）负责：
    - 把自己的数据源转成 DriftInputs
    - 决定对 DriftReport 做什么（print / halt / publish 到 NATS）
    """
    subscores: dict[str, SubscoreReport] = {}
    for category in ("financial", "execution", "decision", "data"):
        subscores[category] = _build_subscore(category, inputs)

    # 加权和 * 4 → [0, 8] 区间
    weighted = sum(
        sub.value * _CATEGORY_WEIGHTS[name]
        for name, sub in subscores.items()
    )
    total = round(weighted * _WEIGHT_SCALE)
    total = max(_TOTAL_MIN, min(_TOTAL_MAX, total))

    state = _state_from_total(total)

    has_missing = any(
        ind.missing
        for sub in subscores.values()
        for ind in sub.indicators
    )

    allow_upgrade = _allow_upgrade_from(total, has_missing)
    abort_action = _abort_action_from(total, subscores)

    notes = _compose_notes(inputs, subscores, has_missing, state)

    nominal = STAGE_NOMINAL_USDT[inputs.stage]
    evaluated = inputs.evaluated_at
    if evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=timezone.utc)

    return DriftReport(
        schema_version=SCHEMA_VERSION,
        evaluated_at=evaluated,
        stage=inputs.stage,
        nominal_scale_usdt=nominal,
        window_hours=inputs.window_hours,
        subscores=subscores,
        total_score=total,
        state=state,
        allow_ladder_upgrade=allow_upgrade,
        abort_hook_action=abort_action,
        notes=notes,
    )


def _compose_notes(
    inputs: DriftInputs,
    subscores: dict[str, SubscoreReport],
    has_missing: bool,
    state: DriftState,
) -> list[str]:
    """生成给 operator 看的自然语言提示。

    逻辑很简单：枚举每个 critical / warning 指标，对应一条中文提示；
    加上 has_missing 和 state 级别的整体提示。不是诊断系统，只是提醒
    operator 看哪里。
    """
    notes: list[str] = []

    if has_missing:
        missing_names = [
            ind.name
            for sub in subscores.values()
            for ind in sub.indicators
            if ind.missing
        ]
        notes.append(
            "missing data: "
            + ", ".join(missing_names)
            + "（数据源不可用，已按 0 分计入但禁止升阶梯）"
        )

    for sub in subscores.values():
        for ind in sub.indicators:
            if ind.normalized == 2 and not ind.missing:
                notes.append(
                    f"{ind.name}={_serialize_raw(ind.raw)} 已越过 critical 阈值"
                )
            elif ind.normalized == 1 and not ind.missing:
                notes.append(
                    f"{ind.name}={_serialize_raw(ind.raw)} 在 warning 区间，继续观察"
                )

    # 整体提示
    if state == "clean":
        notes.append("total=0 全绿，阶梯升级条件 §4.4 满足")
    elif state == "minor_drift":
        notes.append("total=1 有小瑕疵但可升阶梯，operator 请在决策日志里记录")
    elif state in ("noticeable_drift", "significant_drift"):
        notes.append("total ≥ 2 禁止升阶梯，原地继续观察")
    elif state in ("severe_drift", "critical_drift"):
        notes.append("total ≥ 4 建议立即人工复核 + 考虑 halt")

    # 透传调用方注入的 notes
    notes.extend(inputs.notes)

    return notes


__all__ = [
    "SCHEMA_VERSION",
    "STAGE_NOMINAL_USDT",
    "StageTier",
    "DriftInputs",
    "DriftReport",
    "DriftState",
    "AbortHookAction",
    "SubscoreReport",
    "IndicatorReport",
    "compute_drift_score",
]
