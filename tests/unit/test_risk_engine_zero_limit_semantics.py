"""2026-04-21 C2 anchor test #2 · 位置/名义上限 = 0 的当前语义。

## 背景

运营在 `.env` 或 API 里设置位置/名义上限**误设为 0**是可能的。系统
对 "设 0" 的解读**不一致**：

| 字段 | 代码行为（当前） | 是否直觉一致 |
|------|----------------|------------|
| `max_abs_position_qty=0` | `max_abs_qty = 0 * budget = 0`，`capped_qty` 被夹成 0 → 硬拒开仓 | ✅ 安全（恰好） |
| `max_notional_per_symbol=0` | `max_notional=0`；line 113 `if projected_notional > 0 and qty > EPSILON` → 应用 scale=0/projected → capped=0 → 硬拒 | ✅ 安全 |
| `max_gross_notional_per_symbol=0` | line 1453 `if max_gross > Decimal("0") and ...` → **检查直接跳过** → 放行 | ⚠️ 不安全 |
| `max_pending_notional_per_symbol=0` | line 1459 同上 → **检查直接跳过** → 放行 | ⚠️ 不安全 |
| `max_total_open_notional=0` | line 1465 同上 → **检查直接跳过** → 放行 | ⚠️ 不安全 |
| `max_daily_realized_loss_usdt=0` | line 1469 同上 → **检查直接跳过** → 放行 | ⚠️ 不安全 |

## 这个 anchor test 的目的

1. **锁定当前行为**：未来有人改 0 的语义（例如把 line 1453 的 `> 0` 去
   掉），行为变化会立刻被这个测试抓住
2. **警示运营**：这些测试的 docstring 列明哪些字段设 0 = "禁用检查"、
   哪些 = "等同无穷小 = 硬拒"。运营同学改配置前可以查。
3. **给未来"修 gap"留空间**：如果日后决定"0 = 禁用"是 bad UX，改成
   "0 = 拒绝所有 open" 需要动 4 行代码；改之前更新本测试 + 加 API 侧
   validation 阻止运营意外设 0。

## 为什么不现在 fix

这是**配置 UX 问题**，不是 runtime 漏洞。生产 `.env.derivatives.live` 里
所有字段都是非零值（见 `aats/bootstrap/settings.py` 默认值 + `.env`
覆盖）。加 fix 前需要：
- 商量 "0 是 disable 还是 enforce zero" 的语义
- 在 AATSSettings 的 model validator 里拒绝 0（Pydantic Field(gt=0)）
- 同步更新文档和调用方

这是**C2 审计的 output**，不是 C2 的 fix。
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import PositionTarget
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.risk import RiskEngine
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository

from tests.unit.test_guarded_live import (
    FakeAccountService,
    FakeExecutionProvider,
    FakeHealthyMarketProvider,
    FakeHealthyReconciliationRepo,
)


def _make_derivatives_settings(**overrides) -> AATSSettings:
    """构造 derivatives 配置；overrides 可以把指定 limit 设为 0 来测试。"""
    base = {
        "trading_product_type": "derivatives",
        "margin_mode": "cross",
        "default_symbol": "BTC-USDT-SWAP",
        "allowed_symbols": ("BTC-USDT-SWAP",),
        # 合理默认（不触发 validator）
        "max_abs_position_qty": 0.2,
        "max_notional_per_symbol": 5_000.0,
        "max_gross_notional_per_symbol": 5_000.0,
        "max_pending_notional_per_symbol": 5_000.0,
        "max_total_open_notional": 10_000.0,
        "max_target_leverage": 5,
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


def _build_risk_engine(settings) -> RiskEngine:
    kill_switch = KillSwitch()
    mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
    account_service = FakeAccountService(symbol="BTC-USDT-SWAP")
    health_service = SystemHealthService(
        settings=settings,
        mode_controller=mode_controller,
        kill_switch=kill_switch,
        market_provider=FakeHealthyMarketProvider(),  # type: ignore[arg-type]
        account_provider=account_service,  # type: ignore[arg-type]
        execution_provider=FakeExecutionProvider(),  # type: ignore[arg-type]
        reconciliation_repo=FakeHealthyReconciliationRepo(),  # type: ignore[arg-type]
    )
    return RiskEngine(
        settings=settings,
        account_service=account_service,  # type: ignore[arg-type]
        health_service=health_service,
        trigger_policy=DecisionTriggerPolicy(settings=settings),
        price_provider=lambda _symbol: Decimal("30000"),
        mode_controller=mode_controller,
        obligation_repo=InMemoryExecutionObligationRepository(),
    )


def _make_open_target(qty: str, notional: str) -> PositionTarget:
    """构造一个想开新仓的 PositionTarget。"""
    return PositionTarget(
        decision_id="zero_limit_test",
        symbol="BTC-USDT-SWAP",
        target_position_qty=Decimal(qty),
        current_position_qty=Decimal("0"),
        delta_position_qty=Decimal(qty),
        current_notional=Decimal("0"),
        target_notional=Decimal(notional),
        rebalance_reason="test_zero_limits",
        urgency="low",
        max_slippage_tolerance_bps=20,
        source_mix={"baseline": 1.0},
        decision_expiry_ts=utc_now(),
        product_type="derivatives",
        target_leverage=3.0,
        margin_mode="cross",
    )


class TestMaxAbsPositionQtyZero(unittest.TestCase):
    """`max_abs_position_qty=0` → 所有 open 被硬夹成 0 qty → 等同硬拒。"""

    def test_zero_caps_quantity_to_zero(self) -> None:
        settings = _make_derivatives_settings(max_abs_position_qty=0)
        risk = _build_risk_engine(settings)
        target = _make_open_target(qty="0.05", notional="1500")
        decision = risk.evaluate(target)
        # capped_qty 被夹成 0，且 max_abs_qty constraint 应用上
        self.assertEqual(
            decision.capped_target_position_qty,
            Decimal("0"),
            "max_abs_position_qty=0 应让 capped_qty=0（硬拒 open）",
        )
        self.assertIn("max_abs_qty", decision.constraints_applied)


class TestMaxNotionalPerSymbolZero(unittest.TestCase):
    """`max_notional_per_symbol=0` → projected_notional>0 时被 scale 到 0 → 硬拒。

    line 113：`if projected_notional > max_notional and abs(target_qty) > EPSILON`
    → `notional_scale = max_notional / projected_notional = 0 / positive = 0`
    → `capped_qty *= 0` = 0
    """

    def test_zero_notional_scales_to_zero(self) -> None:
        settings = _make_derivatives_settings(max_notional_per_symbol=0)
        risk = _build_risk_engine(settings)
        target = _make_open_target(qty="0.05", notional="1500")
        decision = risk.evaluate(target)
        self.assertEqual(
            decision.capped_target_position_qty,
            Decimal("0"),
            "max_notional_per_symbol=0 应让 capped_qty 被 scale 到 0",
        )
        self.assertIn("max_notional_per_symbol", decision.constraints_applied)


class TestMaxGrossNotionalZeroDisablesCheck(unittest.TestCase):
    """⚠️ `max_gross_notional_per_symbol=0` 当前**禁用**该检查，不是硬拒。

    line 1453: `if max_gross_notional_per_symbol > Decimal("0") and ...`
    → 值为 0 时 `> 0` 为 False → if 整体 False → 检查跳过

    这导致"运营想设硬上限为 0 以禁止该 symbol"的意图**无效**。未来若
    修此 UX gap，需同步：
      1. 改 line 1453/1459/1465/1469 的 `> 0` gate
      2. 在 AATSSettings validator 里加 `Field(gt=0)` 防止误设
      3. 更新本测试（预期从"检查跳过"改成"硬拒 open"）
    """

    def test_zero_gross_limit_does_not_reject_opens(self) -> None:
        settings = _make_derivatives_settings(max_gross_notional_per_symbol=0)
        risk = _build_risk_engine(settings)
        target = _make_open_target(qty="0.05", notional="1500")
        decision = risk.evaluate(target)
        # 注意：projected_notional=1500，如果 gross=0 启用会被拒；
        # 实际行为：检查跳过 → 走其他 gate（可能被 margin / recovery /
        # FakeAccountService 其他路径拒），但**不会因"max_gross=0"而被拒**。
        self.assertNotIn(
            "max_gross_notional_per_symbol_exceeded",
            decision.rejection_reasons,
            "max_gross=0 当前被解读为『禁用该检查』，不会触发 exceeded reason",
        )


class TestMaxPendingNotionalZeroDisablesCheck(unittest.TestCase):
    """⚠️ 同上：`max_pending_notional_per_symbol=0` 禁用检查。"""

    def test_zero_pending_limit_disables_check(self) -> None:
        settings = _make_derivatives_settings(max_pending_notional_per_symbol=0)
        risk = _build_risk_engine(settings)
        target = _make_open_target(qty="0.05", notional="1500")
        decision = risk.evaluate(target)
        self.assertNotIn(
            "max_pending_notional_per_symbol_exceeded",
            decision.rejection_reasons,
            "max_pending=0 当前被解读为『禁用该检查』",
        )


class TestMaxTotalOpenNotionalZeroDisablesCheck(unittest.TestCase):
    """⚠️ 同上：`max_total_open_notional=0` 禁用检查。"""

    def test_zero_total_open_limit_disables_check(self) -> None:
        settings = _make_derivatives_settings(max_total_open_notional=0)
        risk = _build_risk_engine(settings)
        target = _make_open_target(qty="0.05", notional="1500")
        decision = risk.evaluate(target)
        self.assertNotIn(
            "max_total_open_notional_exceeded",
            decision.rejection_reasons,
            "max_total_open=0 当前被解读为『禁用该检查』",
        )


class TestZeroLimitSemanticsSummary(unittest.TestCase):
    """本测试文件是一个 contract：运营 / 维护者只要读这里的断言就知道
    每个字段设 0 的 当前 含义。改行为前必须更新断言 + docstring。
    """

    def test_semantics_documentation_anchor(self) -> None:
        """锚点：本文件的 docstring 必须和测试断言保持一致。

        TODO 未来如果决定把 "0 = 禁用" 改成 "0 = 硬拒"：
          - 改 risk.py line 1453/1459/1465/1469 的 `> Decimal("0")` gate
          - 或（更好）加 Pydantic Field(gt=0) 阻止设置 0
          - 更新本测试的 4 个 ZeroDisablesCheck 测试类
          - 更新本文件顶部的语义对照表
        """
        self.assertTrue(True)  # Anchor marker


if __name__ == "__main__":
    unittest.main()
