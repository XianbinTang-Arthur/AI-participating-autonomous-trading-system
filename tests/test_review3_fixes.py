#!/usr/bin/env python3
"""第三轮 Review 修复验证测试.

验证 6 项修复:
  P0:   funding_rate=Decimal("0") 不再被当作 None
  P1-1: scale_in_threshold >= entry_threshold 约束
  P1-2: _compute_position_delta 移除未使用参数
  P2-1: directional adapter _advance_state 统一 keyword-only + 命名
  P2-2: max_thesis_age_seconds 类型统一为 float
  P2-3: blocking_reasons 副作用注释
"""

import inspect
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.data_platform.replay.core.replay_context import (
    ReplayBar,
    ReplayBarContext,
    ReplayDecision,
    ReplayParameterOverrides,
    ReplayState,
)
from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter
from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name} — {detail}")
        failed += 1


# ══════════════════════════════════════════════════════════════
# 辅助：创建测试用 bar
# ══════════════════════════════════════════════════════════════

def _make_bar(
    ts: datetime,
    close: str = "100.0",
    funding_rate: str | None = "0.0001",
) -> ReplayBar:
    fr = Decimal(funding_rate) if funding_rate is not None else None
    return ReplayBar(
        symbol="BTC-USDT-SWAP",
        ts=ts,
        open=Decimal("99.0"),
        high=Decimal("101.0"),
        low=Decimal("98.0"),
        close=Decimal(close),
        volume=Decimal("1000"),
        quote_volume=Decimal("100000"),
        is_closed=True,
        aligned_funding_rate=fr,
        funding_source_ts=ts,
    )


# ══════════════════════════════════════════════════════════════
# P0: funding_rate = Decimal("0") 不再被当作 None
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P0: funding_rate 零值判断修复")
print("=" * 60)

# 准备一个有足够历史的 adapter
base_ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)

# --- Independent adapter ---
ind_adapter = IndependentReplayAdapter()
ind_state = ind_adapter.reset_state()
params = ReplayParameterOverrides()

# 灌入足够 bar 以产生评分
for i in range(25):
    ts = datetime(2026, 4, 1, 12, i, 0, tzinfo=timezone.utc)
    bar = _make_bar(ts, close=str(100.0 + i * 0.1), funding_rate="0")
    ctx = ReplayBarContext(
        bar=bar, bar_index=i, state=ind_state, params=params,
        family="independent", symbol="BTC-USDT-SWAP", timeframe="15m",
        dataset_version="v1.0",
    )
    decision = ind_adapter.evaluate_bar(ctx)

# 最后一个 decision 的 funding_rate 应该是 0.0，不是 None
check(
    "Independent: funding_rate=Decimal('0') → 0.0 不是 None",
    decision.funding_rate == 0.0,
    f"got {decision.funding_rate!r}",
)

# --- Directional adapter ---
dir_adapter = DirectionalReplayAdapter()
dir_state = dir_adapter.reset_state()
dir_params = ReplayParameterOverrides.for_family("directional")

for i in range(25):
    ts = datetime(2026, 4, 1, 12, i, 0, tzinfo=timezone.utc)
    bar = _make_bar(ts, close=str(100.0 + i * 0.1), funding_rate="0")
    ctx = ReplayBarContext(
        bar=bar, bar_index=i, state=dir_state, params=dir_params,
        family="directional", symbol="BTC-USDT-SWAP", timeframe="15m",
        dataset_version="v1.0",
    )
    dir_decision = dir_adapter.evaluate_bar(ctx)

check(
    "Directional: funding_rate=Decimal('0') → 0.0 不是 None",
    dir_decision.funding_rate == 0.0,
    f"got {dir_decision.funding_rate!r}",
)

# --- funding_rate=None 时仍正确返回 None ---
ind_adapter2 = IndependentReplayAdapter()
ind_state2 = ind_adapter2.reset_state()
for i in range(25):
    ts = datetime(2026, 4, 1, 13, i, 0, tzinfo=timezone.utc)
    bar = _make_bar(ts, close=str(100.0 + i * 0.1), funding_rate=None)
    ctx = ReplayBarContext(
        bar=bar, bar_index=i, state=ind_state2, params=params,
        family="independent", symbol="BTC-USDT-SWAP", timeframe="15m",
        dataset_version="v1.0",
    )
    decision_none = ind_adapter2.evaluate_bar(ctx)

check(
    "Independent: funding_rate=None 时保持 None",
    decision_none.funding_rate is None,
    f"got {decision_none.funding_rate!r}",
)

print()

# ══════════════════════════════════════════════════════════════
# P1-1: scale_in_threshold >= entry_threshold 约束
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P1-1: scale_in_threshold >= entry_threshold 约束")
print("=" * 60)

# 正常情况：scale_in >= entry
try:
    p = ReplayParameterOverrides(entry_threshold=0.40, scale_in_threshold=0.60)
    check("scale_in=0.60, entry=0.40 — 通过", True)
except ValueError:
    check("scale_in=0.60, entry=0.40 — 通过", False, "不应抛异常")

# 边界：scale_in == entry
try:
    p = ReplayParameterOverrides(entry_threshold=0.40, scale_in_threshold=0.40)
    check("scale_in=0.40, entry=0.40（相等）— 通过", True)
except ValueError:
    check("scale_in=0.40, entry=0.40（相等）— 通过", False, "不应抛异常")

# 违规：scale_in < entry
try:
    p = ReplayParameterOverrides(entry_threshold=0.40, scale_in_threshold=0.30)
    check("scale_in=0.30, entry=0.40 — 抛 ValueError", False, "未抛异常")
except ValueError as e:
    check("scale_in=0.30, entry=0.40 — 抛 ValueError", True)
    check("错误消息包含 scale_in_threshold", "scale_in_threshold" in str(e))

# from_dict 也受保护
try:
    p = ReplayParameterOverrides.from_dict({
        "entry_threshold": 0.50,
        "scale_in_threshold": 0.30,
    })
    check("from_dict scale_in < entry — 抛 ValueError", False, "未抛异常")
except ValueError:
    check("from_dict scale_in < entry — 抛 ValueError", True)

print()

# ══════════════════════════════════════════════════════════════
# P1-2: _compute_position_delta 签名
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P1-2: _compute_position_delta 签名简化")
print("=" * 60)

sig = inspect.signature(IndependentReplayAdapter._compute_position_delta)
param_names = list(sig.parameters.keys())

check(
    "参数只有 self, state, action",
    param_names == ["self", "state", "action"],
    f"got {param_names}",
)
check(
    "无 dominant_leg 参数",
    "dominant_leg" not in param_names,
)
check(
    "无 bar 参数",
    "bar" not in param_names,
)

print()

# ══════════════════════════════════════════════════════════════
# P2-1: directional _advance_state 统一为 keyword-only
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P2-1: directional _advance_state keyword-only 统一")
print("=" * 60)

dir_sig = inspect.signature(DirectionalReplayAdapter._advance_state)
dir_params_list = list(dir_sig.parameters.keys())
dir_kinds = {
    name: param.kind
    for name, param in dir_sig.parameters.items()
}

# 除 self 外，所有参数应为 KEYWORD_ONLY
non_self_params = {k: v for k, v in dir_kinds.items() if k != "self"}
all_kw_only = all(
    v == inspect.Parameter.KEYWORD_ONLY
    for v in non_self_params.values()
)
check(
    "directional _advance_state 非 self 参数全为 keyword-only",
    all_kw_only,
    f"got kinds: {non_self_params}",
)

# 变量名统一检查
check(
    "使用 dominant_score 而非 score",
    "dominant_score" in dir_params_list,
    f"params: {dir_params_list}",
)
check(
    "使用 execution_compatible 而非 exec_ok",
    "execution_compatible" in dir_params_list,
    f"params: {dir_params_list}",
)

# Independent 也是 keyword-only（对照）
ind_sig = inspect.signature(IndependentReplayAdapter._advance_state)
ind_kinds = {
    name: param.kind
    for name, param in ind_sig.parameters.items()
    if name != "self"
}
all_ind_kw = all(
    v == inspect.Parameter.KEYWORD_ONLY
    for v in ind_kinds.values()
)
check(
    "independent _advance_state 也是 keyword-only（一致性）",
    all_ind_kw,
)

print()

# ══════════════════════════════════════════════════════════════
# P2-2: max_thesis_age_seconds 类型统一
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P2-2: max_thesis_age_seconds 类型为 float")
print("=" * 60)

# 字段默认值
p_default = ReplayParameterOverrides()
check(
    "默认值类型为 float",
    isinstance(p_default.max_thesis_age_seconds, float),
    f"got type={type(p_default.max_thesis_age_seconds).__name__}",
)
check(
    "默认值 == 1800.0",
    p_default.max_thesis_age_seconds == 1800.0,
)

# from_dict 反序列化
p_from = ReplayParameterOverrides.from_dict({"max_thesis_age_seconds": 3600})
check(
    "from_dict(3600) → float",
    isinstance(p_from.max_thesis_age_seconds, float),
    f"got type={type(p_from.max_thesis_age_seconds).__name__}",
)

# to_dict 序列化
d = p_default.to_dict()
check(
    "to_dict 输出 float",
    isinstance(d["max_thesis_age_seconds"], float),
    f"got type={type(d['max_thesis_age_seconds']).__name__}",
)

# 与其他时间字段类型一致
check(
    "min_hold_seconds 同为 float",
    type(p_default.min_hold_seconds) == type(p_default.max_thesis_age_seconds),
)
check(
    "rebalance_cooldown_seconds 同为 float",
    type(p_default.rebalance_cooldown_seconds) == type(p_default.max_thesis_age_seconds),
)

print()

# ══════════════════════════════════════════════════════════════
# P2-3: blocking_reasons 副作用注释
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P2-3: blocking_reasons 副作用文档化")
print("=" * 60)

ind_source = Path(ROOT / "aats/data_platform/replay/adapters/independent_adapter.py").read_text(encoding="utf-8")
dir_source = Path(ROOT / "aats/data_platform/replay/adapters/directional_adapter.py").read_text(encoding="utf-8")

check(
    "Independent: 调用处有副作用注释",
    "副作用" in ind_source and "blocking_reasons" in ind_source,
)
check(
    "Directional: 调用处有副作用注释",
    "副作用" in dir_source and "blocking_reasons" in dir_source,
)
check(
    "Directional: _advance_state docstring 提到副作用",
    "副作用传参" in dir_source,
)

print()

# ══════════════════════════════════════════════════════════════
# 状态机功能回归：确保重构不破坏行为
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("状态机行为回归测试")
print("=" * 60)

# --- Independent: thesis_failed 触发 ---
ind_reg = IndependentReplayAdapter()
ind_reg_state = ind_reg.reset_state()
params_reg = ReplayParameterOverrides(
    failed_thesis_net_edge_bps=-1.0,
    de_risk_net_edge_bps=2.0,
    min_hold_seconds=60.0,
    max_thesis_age_seconds=86400.0,
)

# 灌入 25 根 bar 建立历史
for i in range(25):
    ts = datetime(2026, 4, 1, 14, i, 0, tzinfo=timezone.utc)
    bar = _make_bar(ts, close=str(100.0 + i * 0.5))
    ctx = ReplayBarContext(
        bar=bar, bar_index=i, state=ind_reg_state, params=params_reg,
        family="independent", symbol="BTC-USDT-SWAP", timeframe="15m",
        dataset_version="v1.0",
    )
    d = ind_reg.evaluate_bar(ctx)

# 确认 adapter 还能正常工作（不抛异常）
check("Independent 回归: 25 bar 无异常", True)

# --- Directional: 正常运行回归 ---
dir_reg = DirectionalReplayAdapter()
dir_reg_state = dir_reg.reset_state()
dir_params_reg = ReplayParameterOverrides.for_family("directional")

for i in range(25):
    ts = datetime(2026, 4, 1, 15, i, 0, tzinfo=timezone.utc)
    bar = _make_bar(ts, close=str(100.0 + i * 0.3))
    ctx = ReplayBarContext(
        bar=bar, bar_index=i, state=dir_reg_state, params=dir_params_reg,
        family="directional", symbol="BTC-USDT-SWAP", timeframe="15m",
        dataset_version="v1.0",
    )
    d = dir_reg.evaluate_bar(ctx)

check("Directional 回归: 25 bar 无异常", True)
check(
    "Directional decision 字段完整",
    hasattr(d, "action") and hasattr(d, "close_reason") and hasattr(d, "funding_rate"),
)

print()

# ══════════════════════════════════════════════════════════════
# 总结
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print(f"验证结果: {passed} passed, {failed} failed")
print("=" * 60)

if __name__ == "__main__":
    if failed > 0:
        print("\n[FAIL] 有验证项未通过!")
        sys.exit(1)
    else:
        print("\n[ALL PASS] 第三轮 review 修复全部验证通过!")
        sys.exit(0)
