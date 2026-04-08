"""Stage 9 drift_score 纯函数单元测试。

覆盖范围
========

1. 输入结构校验
   - 未知 stage 抛 ValueError
   - window_hours ≤ 0 抛 ValueError

2. 归一化阈值
   - 正向指标（越小越好）各档位映射
   - 反向指标（越大越好）各档位映射 (fill_success / decision_cadence)
   - 缺数据（None）归一化为 0 并标记 missing

3. 子类聚合
   - 单个子类全零 → subscore.value == 0
   - 单个子类全 2 → subscore.value == 2
   - 混合指标 → mean 正确

4. 总分聚合
   - 全零输入 → total=0 clean, allow_upgrade=True, abort=none
   - 全一输入 → total=4 severe_drift, abort=halt_on_repeat
   - 全二输入 → total=8 critical_drift, abort=halt_immediate
   - total=5 → halt_immediate (score≥5 规则)
   - 单个 category 全 critical → abort=halt_immediate (subscore_2 规则)
     即使 total 没到 5 也触发

5. 允许升阶梯规则
   - total=0 无 missing → allow=True
   - total=0 有 missing → allow=False
   - total=1 无 missing → allow=True
   - total=2 → allow=False (不管有没有 missing)

6. DriftReport.to_dict()
   - schema_version 字段存在
   - 所有子类都出现在 subscores 字典里
   - evaluated_at 是 ISO 字符串
   - raw 字段 Decimal 序列化为 str

7. Notes 生成
   - missing 指标会被列出来
   - critical 指标有 "越过 critical 阈值" 的提示
   - warning 指标有 "在 warning 区间" 的提示
   - 调用方注入的 notes 会被透传

这些测试**不**依赖任何真实数据源，全部用 in-memory DriftInputs 驱动。
与 drift_inputs.py / abort_hooks.py 完全解耦。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aats.services.governance_engine.drift_score import (
    DriftInputs,
    DriftReport,
    SCHEMA_VERSION,
    STAGE_NOMINAL_USDT,
    compute_drift_score,
)


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc)


def _mk(**kwargs) -> DriftInputs:
    """构造一个默认 DriftInputs —— 所有指标都是 None，窗口 24h，T2。

    测试用例用 kwargs override 自己关心的字段。这样每条测试都显式看见
    自己在测什么字段。
    """
    defaults = dict(
        stage="T2",
        window_hours=24,
        evaluated_at=_NOW,
    )
    defaults.update(kwargs)
    return DriftInputs(**defaults)


def _clean_numbers() -> dict:
    """一组全在 clean 区间的指标值。用于测 "全零输入 → total=0"。"""
    return dict(
        balance_drift_ratio=Decimal("0.005"),
        max_drawdown_ratio=Decimal("0.02"),
        fee_to_pnl_ratio=Decimal("0.20"),
        fill_success_ratio=Decimal("0.99"),
        adverse_slippage_ratio=Decimal("0.01"),
        decision_cycle_cadence_ratio=Decimal("0.98"),
        decision_error_ratio=Decimal("0.005"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0005"),
        okx_rate_limit_count=0,
    )


def _warning_numbers() -> dict:
    """一组全在 warning 区间（归一化为 1）的值。"""
    return dict(
        balance_drift_ratio=Decimal("0.03"),      # 0.01 < x ≤ 0.05
        max_drawdown_ratio=Decimal("0.04"),       # 0.03 < x ≤ 0.05
        fee_to_pnl_ratio=Decimal("0.45"),         # 0.30 < x ≤ 0.60
        fill_success_ratio=Decimal("0.93"),       # 0.90 ≤ x < 0.98 (反向)
        adverse_slippage_ratio=Decimal("0.05"),   # 0.02 < x ≤ 0.10
        decision_cycle_cadence_ratio=Decimal("0.85"),  # 0.80 ≤ x < 0.95 (反向)
        decision_error_ratio=Decimal("0.03"),     # 0.01 < x ≤ 0.05
        reconciliation_mismatch_count=2,           # 0 < x ≤ 2
        nats_handler_error_ratio=Decimal("0.005"),# 0.001 < x ≤ 0.01
        okx_rate_limit_count=3,                    # 0 < x ≤ 3
    )


def _critical_numbers() -> dict:
    """一组全在 critical 区间（归一化为 2）的值。"""
    return dict(
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
    )


# ─────────────────────────────────────────────────────────────────────
# 1. 输入结构校验
# ─────────────────────────────────────────────────────────────────────


def test_unknown_stage_raises() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        DriftInputs(stage="T99", window_hours=24, evaluated_at=_NOW)  # type: ignore[arg-type]


def test_zero_window_hours_raises() -> None:
    with pytest.raises(ValueError, match="window_hours"):
        DriftInputs(stage="T1", window_hours=0, evaluated_at=_NOW)


def test_negative_window_hours_raises() -> None:
    with pytest.raises(ValueError, match="window_hours"):
        DriftInputs(stage="T1", window_hours=-1, evaluated_at=_NOW)


def test_stage_nominal_table_has_all_tiers() -> None:
    for tier in ("T0", "T1", "T2", "T3", "T4"):
        assert tier in STAGE_NOMINAL_USDT


# ─────────────────────────────────────────────────────────────────────
# 2. 归一化阈值
# ─────────────────────────────────────────────────────────────────────


def test_positive_indicator_clean() -> None:
    inp = _mk(balance_drift_ratio=Decimal("0.005"))
    report = compute_drift_score(inp)
    fin = report.subscores["financial"]
    bdi = next(i for i in fin.indicators if i.name == "balance_drift_ratio")
    assert bdi.normalized == 0
    assert bdi.missing is False


def test_positive_indicator_warning_exact_threshold() -> None:
    # warning 阈值 0.01：x ≤ 0.01 → 0 分
    inp = _mk(balance_drift_ratio=Decimal("0.01"))
    report = compute_drift_score(inp)
    fin = report.subscores["financial"]
    bdi = next(i for i in fin.indicators if i.name == "balance_drift_ratio")
    assert bdi.normalized == 0


def test_positive_indicator_warning_band() -> None:
    inp = _mk(balance_drift_ratio=Decimal("0.03"))
    report = compute_drift_score(inp)
    fin = report.subscores["financial"]
    bdi = next(i for i in fin.indicators if i.name == "balance_drift_ratio")
    assert bdi.normalized == 1


def test_positive_indicator_critical() -> None:
    inp = _mk(balance_drift_ratio=Decimal("0.10"))
    report = compute_drift_score(inp)
    fin = report.subscores["financial"]
    bdi = next(i for i in fin.indicators if i.name == "balance_drift_ratio")
    assert bdi.normalized == 2


def test_reverse_indicator_fill_success_clean() -> None:
    # fill_success_ratio 是反向指标，0.99 ≥ 0.98 → 0 分
    inp = _mk(fill_success_ratio=Decimal("0.99"))
    report = compute_drift_score(inp)
    ex = report.subscores["execution"]
    fs = next(i for i in ex.indicators if i.name == "fill_success_ratio")
    assert fs.normalized == 0


def test_reverse_indicator_fill_success_warning() -> None:
    # 0.93 在 [0.90, 0.98) 之间 → 1 分
    inp = _mk(fill_success_ratio=Decimal("0.93"))
    report = compute_drift_score(inp)
    ex = report.subscores["execution"]
    fs = next(i for i in ex.indicators if i.name == "fill_success_ratio")
    assert fs.normalized == 1


def test_reverse_indicator_fill_success_critical() -> None:
    # 0.85 < 0.90 → 2 分
    inp = _mk(fill_success_ratio=Decimal("0.85"))
    report = compute_drift_score(inp)
    ex = report.subscores["execution"]
    fs = next(i for i in ex.indicators if i.name == "fill_success_ratio")
    assert fs.normalized == 2


def test_reverse_indicator_cadence_clean() -> None:
    inp = _mk(decision_cycle_cadence_ratio=Decimal("0.99"))
    report = compute_drift_score(inp)
    dec = report.subscores["decision"]
    cad = next(i for i in dec.indicators if i.name == "decision_cycle_cadence_ratio")
    assert cad.normalized == 0


def test_reverse_indicator_cadence_critical() -> None:
    inp = _mk(decision_cycle_cadence_ratio=Decimal("0.50"))
    report = compute_drift_score(inp)
    dec = report.subscores["decision"]
    cad = next(i for i in dec.indicators if i.name == "decision_cycle_cadence_ratio")
    assert cad.normalized == 2


def test_missing_indicator_is_zero_with_missing_flag() -> None:
    inp = _mk()  # 全部 None
    report = compute_drift_score(inp)
    for sub in report.subscores.values():
        for ind in sub.indicators:
            assert ind.normalized == 0
            assert ind.missing is True


def test_integer_indicator_reconciliation() -> None:
    """reconciliation_mismatch_count 是 int 不是 Decimal，归一化必须处理
    int 输入。"""
    inp = _mk(reconciliation_mismatch_count=5)
    report = compute_drift_score(inp)
    data = report.subscores["data"]
    rm = next(i for i in data.indicators if i.name == "reconciliation_mismatch_count")
    assert rm.normalized == 2  # 5 > 2


def test_integer_indicator_rate_limit() -> None:
    inp = _mk(okx_rate_limit_count=2)
    report = compute_drift_score(inp)
    data = report.subscores["data"]
    rl = next(i for i in data.indicators if i.name == "okx_rate_limit_count")
    assert rl.normalized == 1  # 0 < 2 ≤ 3


# ─────────────────────────────────────────────────────────────────────
# 3. 子类聚合
# ─────────────────────────────────────────────────────────────────────


def test_subscore_all_zero_is_zero() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    for sub in report.subscores.values():
        assert sub.value == 0.0


def test_subscore_all_two_is_two() -> None:
    inp = _mk(**_critical_numbers())
    report = compute_drift_score(inp)
    for sub in report.subscores.values():
        assert sub.value == 2.0


def test_subscore_mixed_mean() -> None:
    """financial 类有 3 个指标：0 + 1 + 2 → mean = 1.0."""
    inp = _mk(
        balance_drift_ratio=Decimal("0.005"),   # 0
        max_drawdown_ratio=Decimal("0.04"),     # 1
        fee_to_pnl_ratio=Decimal("0.80"),       # 2
    )
    report = compute_drift_score(inp)
    fin = report.subscores["financial"]
    assert fin.value == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────
# 4. 总分聚合
# ─────────────────────────────────────────────────────────────────────


def test_total_zero_clean() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    assert report.total_score == 0
    assert report.state == "clean"
    assert report.allow_ladder_upgrade is True
    assert report.abort_hook_action == "none"


def test_total_max_critical() -> None:
    inp = _mk(**_critical_numbers())
    report = compute_drift_score(inp)
    # 全 2 分 × 权重 × 4 = 2*(1/3)*4 + 2*(1/4)*4 + 2*(1/4)*4 + 2*(1/6)*4
    # = 2.67 + 2 + 2 + 1.33 = 8.0 (round = 8)
    assert report.total_score == 8
    assert report.state == "critical_drift"
    assert report.allow_ladder_upgrade is False
    assert report.abort_hook_action == "halt_immediate"


def test_total_mid_range_halt_on_repeat() -> None:
    """所有子项 = 1（warning）→ total = 1*(1/3+1/4+1/4+1/6)*4 = 1*1*4 = 4
    → severe_drift → halt_on_repeat."""
    inp = _mk(**_warning_numbers())
    report = compute_drift_score(inp)
    assert report.total_score == 4
    assert report.state == "severe_drift"
    assert report.abort_hook_action == "halt_on_repeat"


def test_single_category_critical_triggers_halt_immediate() -> None:
    """financial 类三项全 critical，其他类全 clean。

    financial subscore = 2, 其他 = 0
    total = 2 * (1/3) * 4 = 2.67 → round = 3
    但是 subscore=2 规则要求 halt_immediate（§4.4 / §5.2 subscore_financial_2）
    """
    data = _clean_numbers()
    data["balance_drift_ratio"] = Decimal("0.10")
    data["max_drawdown_ratio"] = Decimal("0.08")
    data["fee_to_pnl_ratio"] = Decimal("0.80")
    inp = _mk(**data)
    report = compute_drift_score(inp)
    fin = report.subscores["financial"]
    assert fin.value == 2.0
    assert report.abort_hook_action == "halt_immediate"


def test_score_5_threshold_halt_immediate() -> None:
    """构造一个 total=5 的场景：每一类都是 mean=1.25 左右。
    用 warning + 一个 critical 组合。"""
    inp = _mk(
        # financial: 2+1+1 → mean = 4/3 ≈ 1.33
        balance_drift_ratio=Decimal("0.10"),    # 2
        max_drawdown_ratio=Decimal("0.04"),     # 1
        fee_to_pnl_ratio=Decimal("0.45"),       # 1
        # execution: 1+2 → mean = 1.5
        fill_success_ratio=Decimal("0.93"),     # 1 (反向)
        adverse_slippage_ratio=Decimal("0.20"), # 2
        # decision: 1+2 → mean = 1.5
        decision_cycle_cadence_ratio=Decimal("0.85"),  # 1 (反向)
        decision_error_ratio=Decimal("0.10"),           # 2
        # data: 2+1+1 → mean = 4/3 ≈ 1.33
        reconciliation_mismatch_count=5,         # 2
        nats_handler_error_ratio=Decimal("0.005"),  # 1
        okx_rate_limit_count=2,                  # 1
    )
    report = compute_drift_score(inp)
    # weighted = 1.33*(1/3) + 1.5*(1/4) + 1.5*(1/4) + 1.33*(1/6)
    #          = 0.444 + 0.375 + 0.375 + 0.222
    #          = 1.416
    # total = round(1.416 * 4) = round(5.667) = 6
    # → score ≥ 5 → halt_immediate
    assert report.total_score >= 5
    assert report.abort_hook_action == "halt_immediate"


def test_score_2_warning_no_halt() -> None:
    """单个 warning 指标应得 total ≤ 2，不触发 halt。"""
    inp = _mk(
        **_clean_numbers(),
    )
    # 改一个字段到 warning
    inp_dict = _clean_numbers()
    inp_dict["balance_drift_ratio"] = Decimal("0.03")  # 1 分
    inp = _mk(**inp_dict)
    report = compute_drift_score(inp)
    # financial subscore = 1/3 ≈ 0.33
    # weighted = 0.33 * 1/3 * 4 ≈ 0.444
    # total = round(0.444) = 0
    assert report.total_score == 0
    assert report.abort_hook_action == "none"


# ─────────────────────────────────────────────────────────────────────
# 5. 允许升阶梯规则
# ─────────────────────────────────────────────────────────────────────


def test_allow_upgrade_total_zero() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    assert report.allow_ladder_upgrade is True


def test_allow_upgrade_blocked_when_any_missing() -> None:
    """总分 0 但有 missing 指标 → 禁止升阶梯。"""
    data = _clean_numbers()
    data.pop("balance_drift_ratio")  # 留空 → missing
    inp = _mk(**data)
    report = compute_drift_score(inp)
    assert report.total_score == 0
    assert report.allow_ladder_upgrade is False


def test_allow_upgrade_total_one_still_allowed() -> None:
    """total == 1 minor_drift，仍然允许（与 checklist-1 §4.4 对齐）。"""
    data = _clean_numbers()
    # 把一个指标拉到 warning（1 分），但选权重最重的 financial
    # 让 total 刚好落在 1：financial=0.33, subscore*4/3 = 0.444 → round = 0
    # 需要凑两个 warning 才能把 total 推到 1
    # financial=0.66+execution=0.5: 0.66*1/3*4 + 0.5*1/4*4 = 0.889 + 0.5 = 1.389 → round = 1
    data["balance_drift_ratio"] = Decimal("0.03")      # 1
    data["max_drawdown_ratio"] = Decimal("0.04")       # 1
    data["adverse_slippage_ratio"] = Decimal("0.05")   # 1
    inp = _mk(**data)
    report = compute_drift_score(inp)
    assert report.total_score == 1
    assert report.allow_ladder_upgrade is True


def test_allow_upgrade_total_two_blocked() -> None:
    data = _clean_numbers()
    # 全 financial 都拉到 warning → subscore 1.0 → weighted 1*(1/3)*4 = 1.33 → round 1
    # 再加 execution 全 warning → 1*(1/4)*4 = 1.0 → 累计 2.33 → round 2
    data["balance_drift_ratio"] = Decimal("0.03")
    data["max_drawdown_ratio"] = Decimal("0.04")
    data["fee_to_pnl_ratio"] = Decimal("0.45")
    data["fill_success_ratio"] = Decimal("0.93")
    data["adverse_slippage_ratio"] = Decimal("0.05")
    inp = _mk(**data)
    report = compute_drift_score(inp)
    assert report.total_score == 2
    assert report.state == "noticeable_drift"
    assert report.allow_ladder_upgrade is False
    assert report.abort_hook_action == "warning"


# ─────────────────────────────────────────────────────────────────────
# 6. DriftReport.to_dict() 序列化
# ─────────────────────────────────────────────────────────────────────


def test_to_dict_has_schema_version() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    d = report.to_dict()
    assert d["schema_version"] == SCHEMA_VERSION


def test_to_dict_has_all_subscores() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    d = report.to_dict()
    for category in ("financial", "execution", "decision", "data"):
        assert category in d["subscores"]
        sub = d["subscores"][category]
        assert "value" in sub
        assert "indicators" in sub
        assert len(sub["indicators"]) >= 2  # 每个类至少 2 个


def test_to_dict_evaluated_at_is_iso() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    d = report.to_dict()
    assert d["evaluated_at"].startswith("2026-04-08T11:30:00")


def test_to_dict_raw_decimal_as_str() -> None:
    inp = _mk(balance_drift_ratio=Decimal("0.123456"))
    report = compute_drift_score(inp)
    d = report.to_dict()
    fin = d["subscores"]["financial"]
    bdi = next(i for i in fin["indicators"] if i["name"] == "balance_drift_ratio")
    assert bdi["raw"] == "0.123456"


def test_to_dict_missing_raw_is_none() -> None:
    inp = _mk()
    report = compute_drift_score(inp)
    d = report.to_dict()
    fin = d["subscores"]["financial"]
    for ind in fin["indicators"]:
        assert ind["raw"] is None
        assert ind["missing"] is True


def test_to_dict_nominal_scale_as_str() -> None:
    """Decimal 应序列化为 str，避免 json float 精度损失。"""
    inp = _mk(stage="T4", **_clean_numbers())
    report = compute_drift_score(inp)
    d = report.to_dict()
    assert d["nominal_scale_usdt"] == "1000"


# ─────────────────────────────────────────────────────────────────────
# 7. Notes 生成
# ─────────────────────────────────────────────────────────────────────


def test_notes_mention_missing_fields() -> None:
    inp = _mk()  # 全 None
    report = compute_drift_score(inp)
    joined = " | ".join(report.notes)
    assert "missing data" in joined
    assert "balance_drift_ratio" in joined
    assert "nats_handler_error_ratio" in joined


def test_notes_mention_critical_indicators() -> None:
    inp = _mk(**_critical_numbers())
    report = compute_drift_score(inp)
    joined = " | ".join(report.notes)
    assert "critical" in joined
    assert "balance_drift_ratio" in joined


def test_notes_mention_warning_indicators() -> None:
    inp = _mk(**_warning_numbers())
    report = compute_drift_score(inp)
    joined = " | ".join(report.notes)
    assert "warning" in joined


def test_notes_forward_caller_injected() -> None:
    """调用方传入的 notes 应原样出现在 report.notes 尾部。"""
    data = _clean_numbers()
    inp = DriftInputs(
        stage="T1",
        window_hours=48,
        evaluated_at=_NOW,
        notes=["T1 首次跑, 请 operator 留意"],
        **data,
    )
    report = compute_drift_score(inp)
    assert "T1 首次跑, 请 operator 留意" in report.notes


def test_notes_clean_state_has_green_signal() -> None:
    inp = _mk(**_clean_numbers())
    report = compute_drift_score(inp)
    joined = " | ".join(report.notes)
    assert "total=0" in joined or "全绿" in joined


# ─────────────────────────────────────────────────────────────────────
# 8. 边界：evaluated_at 无 tzinfo 时补 UTC
# ─────────────────────────────────────────────────────────────────────


def test_naive_evaluated_at_gets_utc_tzinfo() -> None:
    naive = datetime(2026, 4, 8, 11, 30, 0)  # 无 tzinfo
    inp = DriftInputs(
        stage="T1",
        window_hours=24,
        evaluated_at=naive,
    )
    report = compute_drift_score(inp)
    assert report.evaluated_at.tzinfo is not None
