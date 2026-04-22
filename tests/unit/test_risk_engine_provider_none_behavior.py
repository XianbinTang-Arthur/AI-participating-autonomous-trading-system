"""2026-04-21 C2 anchor test #1 · Provider returns None behavior.

## 背景

RiskEngine 可以注入 3 个 guard provider：
- `live_runtime_guard_provider`（保证金 / 爆仓 gap / auto_halt 信号）
- `trial_guard_provider`（trial 模式的 breach 信号）
- `recovery_status_provider`（recovery / reconciliation only_reduce 信号）

当 provider **被注入了但 snapshot() 返回 None 或空 dict** 时（例如 Redis
失败、upstream stale、bootstrap 还没完成），RiskEngine 的当前行为是：

```python
# aats/services/governance_engine/risk.py:1796-1804
def _runtime_guard_only_reduce_reasons(self) -> list[str]:
    provider = self.live_runtime_guard_provider
    if provider is None:
        return []
    snapshot_getter = getattr(provider, "snapshot", None)
    payload = snapshot_getter() if callable(snapshot_getter) else None
    if not isinstance(payload, dict) or not bool(payload.get("only_reduce_required")):
        return []    # ← permissive fallback
    ...
```

即：**provider 坏了/失效 → 当作"没要 only_reduce"处理 → 不拒开仓**。

## 为什么这是"设计如此"而不是"漏洞"

1. Provider 是**可选的**（`| None = None`）—— 字段不存在时等价"没装这个 guard"
2. **真正的 fail-closed 在 GuardSignalHotStateCache 层**：它才是 provider 的
   常见实现，在 snapshot 过期/缺失时返回 `_FAIL_CLOSED_SENTINEL`（其中
   `only_reduce_required=True`），RiskEngine 能识别进而硬拒。
3. 测试里我们往往直接传一个 Fake provider（直接返回 `None` 表示"没启
   用"），如果 RiskEngine 在这里变 fail-closed，许多单测会被破坏。

## 本测试锁定的不变性

- provider 注入但 `.snapshot()` 返回 None → RiskEngine 不 raise、不视作
  only_reduce
- provider 没有 `.snapshot()` 方法 → RiskEngine 同样 permissive
- provider 抛异常 → 现在是 **冒泡到调用方**（没有 try/except 包裹）——
  这也是锁定的行为：让上层（调用方）决定如何处理；如果未来有人加
  try/except 想"silently swallow"，那会损害调试能力，测试会红。

## 如果未来想改成 fail-closed

需要**同时**更新：
1. 修 RiskEngine 代码（改成 `return ["provider_returned_none"]`）
2. 改本测试（预期 `approved=False`）
3. **关键**：更新 docs/task/* 说明 "provider 注入必须保证永远不返回 None"
4. 修所有 test fixture（FakeXxxProvider）保证它们返回真实 dict
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


# ── 复用 test_guarded_live.py 已有的 fake 实现 ─────────────────────
from tests.unit.test_guarded_live import (
    FakeAccountService,
    FakeExecutionProvider,
    FakeHealthyMarketProvider,
    FakeHealthyReconciliationRepo,
)


class _ProviderReturnsNone:
    """Provider 注入但 snapshot() 永远返回 None（模拟 upstream 坏掉）。"""

    def snapshot(self) -> None:
        return None


class _ProviderReturnsEmptyDict:
    """Provider 注入但 snapshot() 返回空 dict（模拟 guard 没数据）。"""

    def snapshot(self) -> dict:
        return {}


class _ProviderWithoutSnapshotMethod:
    """Provider 没实现 .snapshot() 方法（模拟接口不完整）。"""

    def some_other_method(self) -> str:
        return "not what RiskEngine expects"


class _ProviderRaisesOnSnapshot:
    """Provider 的 snapshot() 会抛异常（模拟运行时故障）。"""

    def snapshot(self):
        raise RuntimeError("simulated provider failure")


def _make_spot_target() -> PositionTarget:
    """A plain spot target：不走衍生品 pretrade 分支，专注 provider 链路。"""
    return PositionTarget(
        decision_id="provider_none_test",
        symbol="BTC-USDT",
        target_position_qty=Decimal("0.01"),
        current_position_qty=Decimal("0"),
        delta_position_qty=Decimal("0.01"),
        current_notional=Decimal("0"),
        target_notional=Decimal("300"),
        rebalance_reason="test_provider_none",
        urgency="low",
        max_slippage_tolerance_bps=20,
        source_mix={"baseline": 1.0},
        decision_expiry_ts=utc_now(),
    )


def _build_risk_engine_with_runtime_guard(provider) -> RiskEngine:
    """构造一个最小 RiskEngine，注入指定的 live_runtime_guard_provider。

    用 derivatives+cross 配置（spot+cash 要求 unit leverage 限制太紧，
    测试里切 derivatives 更灵活）。用 FakeHealthyMarketProvider 等 stub
    避开真实依赖。本测试只关心 _runtime_guard_only_reduce_reasons()
    和 _recovery_status_payload() 两个方法的纯粹行为。
    """
    settings = AATSSettings.model_validate(
        {
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "default_symbol": "BTC-USDT-SWAP",
            "allowed_symbols": ("BTC-USDT-SWAP",),
            "max_abs_position_qty": 0.2,
            "max_notional_per_symbol": 5_000,
            "max_gross_notional_per_symbol": 5_000,
            "max_pending_notional_per_symbol": 5_000,
            "max_total_open_notional": 10_000,
            "max_target_leverage": 5,
        }
    )
    kill_switch = KillSwitch()
    mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
    account_service = FakeAccountService(symbol="BTC-USDT")
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
        live_runtime_guard_provider=provider,
    )


class TestRuntimeGuardProviderNoneBehavior(unittest.TestCase):
    """`live_runtime_guard_provider` 的各种 None/失效情形。"""

    def test_provider_snapshot_returns_none_does_not_reject_opens(self) -> None:
        """Provider.snapshot() 返回 None → RiskEngine 当作"没装 guard"处理。"""
        risk = _build_risk_engine_with_runtime_guard(_ProviderReturnsNone())
        only_reduce_reasons = risk._runtime_guard_only_reduce_reasons()
        self.assertEqual(
            only_reduce_reasons,
            [],
            "provider.snapshot() 返回 None 时应 permissive（返回 []）。"
            "如果需要改成 fail-closed，见本文件顶部 docstring 的改动清单。",
        )

    def test_provider_snapshot_returns_empty_dict_does_not_reject_opens(self) -> None:
        """Provider.snapshot() 返回 {} → 同上，permissive。"""
        risk = _build_risk_engine_with_runtime_guard(_ProviderReturnsEmptyDict())
        only_reduce_reasons = risk._runtime_guard_only_reduce_reasons()
        self.assertEqual(only_reduce_reasons, [])

    def test_provider_without_snapshot_method_does_not_reject_opens(self) -> None:
        """Provider 没实现 snapshot() → permissive，不 raise。"""
        risk = _build_risk_engine_with_runtime_guard(_ProviderWithoutSnapshotMethod())
        only_reduce_reasons = risk._runtime_guard_only_reduce_reasons()
        self.assertEqual(only_reduce_reasons, [])

    def test_provider_is_none_does_not_reject_opens(self) -> None:
        """Provider 根本没注入（= None）→ permissive。"""
        risk = _build_risk_engine_with_runtime_guard(None)
        only_reduce_reasons = risk._runtime_guard_only_reduce_reasons()
        self.assertEqual(only_reduce_reasons, [])

    def test_provider_snapshot_raises_propagates_exception(self) -> None:
        """Provider.snapshot() 抛异常 → RiskEngine 不捕获，上层可见。

        这是**故意的**：provider 故障应当 visible，不应 silent swallow。
        如果未来有人想加 try/except（想"silently swallow"），会破坏可调试性。
        """
        risk = _build_risk_engine_with_runtime_guard(_ProviderRaisesOnSnapshot())
        with self.assertRaises(RuntimeError) as ctx:
            risk._runtime_guard_only_reduce_reasons()
        self.assertIn("simulated provider failure", str(ctx.exception))


class TestRecoveryStatusProviderNoneBehavior(unittest.TestCase):
    """`recovery_status_provider` 的 None/失效行为。

    recovery_status_provider 是一个 Callable，调用后应返回 dict 或 pydantic model。
    """

    def _build_with_recovery_provider(self, provider):
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_abs_position_qty": 0.2,
                "max_notional_per_symbol": 5_000,
                "max_gross_notional_per_symbol": 5_000,
                "max_pending_notional_per_symbol": 5_000,
                "max_total_open_notional": 10_000,
                "max_target_leverage": 5,
            }
        )
        kill_switch = KillSwitch()
        mode_controller = RuntimeModeController(settings=settings, kill_switch=kill_switch)
        account_service = FakeAccountService(symbol="BTC-USDT")
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
            recovery_status_provider=provider,
        )

    def test_recovery_status_provider_returns_none_means_empty(self) -> None:
        """recovery_status_provider() 返回 None → 等同于 {}（permissive）。"""
        risk = self._build_with_recovery_provider(lambda: None)
        payload = risk._recovery_status_payload()
        self.assertEqual(payload, {})

    def test_recovery_status_provider_returns_non_dict_means_empty(self) -> None:
        """返回非 dict 且没 model_dump → empty dict（permissive，不抛）。"""
        risk = self._build_with_recovery_provider(lambda: "not_a_dict")
        payload = risk._recovery_status_payload()
        self.assertEqual(payload, {})

    def test_recovery_status_provider_is_none_means_empty(self) -> None:
        """recovery_status_provider 根本没注入 → empty。"""
        risk = self._build_with_recovery_provider(None)
        payload = risk._recovery_status_payload()
        self.assertEqual(payload, {})

    def test_recovery_status_provider_raises_propagates(self) -> None:
        """recovery_status_provider() 抛异常 → RiskEngine 不捕获。"""
        def raising_provider():
            raise RuntimeError("simulated recovery provider boom")

        risk = self._build_with_recovery_provider(raising_provider)
        with self.assertRaises(RuntimeError):
            risk._recovery_status_payload()


class TestProviderFailureContract(unittest.TestCase):
    """关键 contract 测试：明确说明"provider 可选"的语义。

    本测试是给未来的维护者看的。它用代码而不是文档的方式说明：**如果
    provider 返回 None / 空，RiskEngine 认为该 guard 没启用**。生产环境
    里，只要 guard_signal_cache 实际注入了，它就会在 stale/empty 时返回
    `_FAIL_CLOSED_SENTINEL`（含 `only_reduce_required=True`），RiskEngine 硬拒。

    因此"guard 静默失效"的真正防线是 guard_signal_cache 的 sentinel，
    不是 RiskEngine 层。这个分层设计的理由：
    - guard_signal_cache 对 stale detection 有更丰富的上下文（TTL、
      _cached_at、跨进程同步）
    - RiskEngine 层应该是"纯决策"，不该去推断"provider 应不应该在线"
    """

    def test_contract_doc_anchor(self) -> None:
        """anchor 存在本身是 contract：如果未来这个测试被删，说明对"provider
        可选"的架构约束正在变。改前必须同步改 guard_signal_cache 和调用者。
        """
        # 这条语句不做什么实际 assert —— 它是 intention marker。
        # 配合上面的 docstring 和顶部 module docstring 形成完整的设计记录。
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
