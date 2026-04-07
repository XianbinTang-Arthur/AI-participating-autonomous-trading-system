"""Stage 3 单元测试：build_runtime 按 process_role 装配 slice。

覆盖三个层次：

1. **纯逻辑** — `_slice_active` / `_SLICE_REQUIRED_ROLES` 的矩阵正确性，
   不依赖 storage 或任何 service 的实例化。
2. **端到端** — 用 storage_mode=memory 的 paper 运行时跑 `build_runtime`，
   按不同 role 验证 ApplicationRuntime 上各 slice 字段的 None / 非 None 状态。
3. **fail-fast** — derivatives+hedge 在 execution-only role 下必须报错，
   防止 leg_risk_evaluator 静默退化为 None。

跨进程拓扑下，被跳过的 slice 字段必须保持为 None，否则 Stage 4 引入 NATS 后
会出现"以为别的进程在跑，结果本地也偷偷跑了一份"的双写隐患。
"""
from __future__ import annotations

import unittest

import pytest

from aats.bootstrap.config import (
    _SLICE_REQUIRED_ROLES,
    _slice_active,
    build_runtime,
)
from aats.bootstrap.settings import (
    AATSSettings,
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
)


# ─────────────────────────────────────────────────────────────────────
# 第 1 层：纯逻辑（_slice_active + _SLICE_REQUIRED_ROLES）
# ─────────────────────────────────────────────────────────────────────


class TestSliceActiveHelper:
    """`_slice_active` 是 build_runtime 内每个 slice builder 顶部的门控点，
    一旦它判错就会出现"应该跑的 slice 没跑"或"不该跑的 slice 跑了"——前者
    会让进程少功能，后者会让两个进程同时改一份状态。所以矩阵必须 exhaustive。
    """

    @pytest.mark.parametrize(
        "role",
        [
            None,
            PROCESS_ROLE_MONOLITH,
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_MARKET,
            PROCESS_ROLE_DECISION,
            PROCESS_ROLE_EXECUTION,
        ],
    )
    def test_shared_slice_active_in_every_role(self, role: str | None) -> None:
        """shared slice 在 None / monolith / gateway / market / decision / execution
        全部 role 下都必须装；它包含 metrics / bus / kill_switch 等所有进程都要的基础设施。"""
        assert _slice_active("shared", effective_process_role=role) is True

    @pytest.mark.parametrize(
        "role,expected",
        [
            (None, True),
            (PROCESS_ROLE_MONOLITH, True),
            (PROCESS_ROLE_MARKET, True),
            (PROCESS_ROLE_GATEWAY, False),
            (PROCESS_ROLE_DECISION, False),
            (PROCESS_ROLE_EXECUTION, False),
        ],
    )
    def test_market_slice_only_in_market_or_monolith(
        self, role: str | None, expected: bool
    ) -> None:
        """market slice (feature_engine) 只在 market 进程或单进程模式下装。"""
        assert _slice_active("market", effective_process_role=role) is expected

    @pytest.mark.parametrize(
        "role,expected",
        [
            (None, True),
            (PROCESS_ROLE_MONOLITH, True),
            (PROCESS_ROLE_DECISION, True),
            (PROCESS_ROLE_GATEWAY, False),
            (PROCESS_ROLE_MARKET, False),
            (PROCESS_ROLE_EXECUTION, False),
        ],
    )
    def test_decision_slice_only_in_decision_or_monolith(
        self, role: str | None, expected: bool
    ) -> None:
        """decision slice (ai_service / decision_engine / risk / policy) 只在
        decision 进程或单进程模式下装。"""
        assert _slice_active("decision", effective_process_role=role) is expected

    @pytest.mark.parametrize(
        "slice_name",
        ["execution", "portfolio", "reconciliation", "startup_recovery"],
    )
    @pytest.mark.parametrize(
        "role,expected",
        [
            (None, True),
            (PROCESS_ROLE_MONOLITH, True),
            (PROCESS_ROLE_EXECUTION, True),
            (PROCESS_ROLE_GATEWAY, False),
            (PROCESS_ROLE_MARKET, False),
            (PROCESS_ROLE_DECISION, False),
        ],
    )
    def test_execution_family_slices_only_in_execution_or_monolith(
        self, slice_name: str, role: str | None, expected: bool
    ) -> None:
        """execution / portfolio / reconciliation / startup_recovery 都和 order_manager
        绑定在同一进程，避免 fill → portfolio 更新跨进程往返。所以这一组要么一起装，
        要么一起跳。"""
        assert _slice_active(slice_name, effective_process_role=role) is expected

    def test_unknown_slice_name_raises_key_error(self) -> None:
        """主动让未知 slice name 抛 KeyError —— 这是早期捕获"新增 slice 但忘了
        把它加进 _SLICE_REQUIRED_ROLES"的故意行为，比沉默地默认 active 安全。"""
        with pytest.raises(KeyError):
            _slice_active("nonexistent_slice", effective_process_role=None)


class TestSliceRequiredRolesMatrix:
    """`_SLICE_REQUIRED_ROLES` 数据本身的不变量。
    这些断言在新增 slice 或新增 role 时会强制提醒维护者更新矩阵。"""

    EXPECTED_SLICES = frozenset(
        {
            "shared",
            "market",
            "decision",
            "execution",
            "portfolio",
            "reconciliation",
            "startup_recovery",
        }
    )

    def test_matrix_covers_all_known_slices(self) -> None:
        assert set(_SLICE_REQUIRED_ROLES.keys()) == self.EXPECTED_SLICES

    @pytest.mark.parametrize(
        "slice_name",
        sorted(EXPECTED_SLICES),
    )
    def test_every_slice_includes_none_and_monolith(self, slice_name: str) -> None:
        """None（向后兼容默认）和 PROCESS_ROLE_MONOLITH（显式单进程）必须永远
        在每个 slice 的 allowed set 里 —— 这是单进程模式不退化的保险丝。"""
        allowed = _SLICE_REQUIRED_ROLES[slice_name]
        assert None in allowed, f"slice {slice_name} 缺少 None"
        assert PROCESS_ROLE_MONOLITH in allowed, f"slice {slice_name} 缺少 monolith"

    @pytest.mark.parametrize(
        "slice_name",
        sorted(EXPECTED_SLICES),
    )
    def test_every_slice_value_is_frozenset(self, slice_name: str) -> None:
        """frozenset 保证表本身在 import 之后不会被偷偷修改 —— 防御 import-time mutation。"""
        assert isinstance(_SLICE_REQUIRED_ROLES[slice_name], frozenset)


# ─────────────────────────────────────────────────────────────────────
# 第 2 层：端到端 build_runtime（每个 role 一次）
# ─────────────────────────────────────────────────────────────────────


def _paper_settings(**overrides: object) -> AATSSettings:
    """构造 paper_live + memory storage 的最小可启动 AATSSettings。

    这套配置不依赖 OKX、不连真数据库、不写盘，是 build_runtime 唯一可在
    单元测试中端到端跑通的形态。
    """
    base = {
        "mode": "paper_live",
        "market_data_backend": "demo",
        "execution_backend": "paper",
        "account_backend": "disabled",
        "account_read_enabled": False,
        "storage_mode": "memory",
        "event_persistence_mode": "strict",
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


class TestBuildRuntimeSliceGating(unittest.IsolatedAsyncioTestCase):
    """端到端验证：每个 process_role 下 ApplicationRuntime 上的 slice 字段
    是否按预期为 None / 非 None。

    这一层测试比纯逻辑更"贵"（要把整个 build_runtime 跑一遍），但能捕获
    Stage 3 引入 None-tolerant 之后被遗漏的字段访问，例如某个后台 loop
    没加 None 检查会在 build_runtime 里炸掉。
    """

    # ── monolith / 默认 role：所有 slice 都装 ─────────────────────

    async def test_default_role_none_builds_full_monolith(self) -> None:
        """process_role 不传 → 等价于 monolith：所有 slice 字段都非 None。"""
        runtime = await build_runtime(_paper_settings())
        # shared 必装
        assert runtime.bus is not None
        assert runtime.market_gateway is not None
        assert runtime.kill_switch is not None
        # market
        assert runtime.feature_engine is not None
        # decision
        assert runtime.ai_service is not None
        assert runtime.decision_engine is not None
        assert runtime.decision_trigger is not None
        assert runtime.risk_engine is not None
        assert runtime.policy_engine is not None
        assert runtime.execution_planner is not None
        # execution
        assert runtime.order_manager is not None
        # portfolio
        assert runtime.portfolio_service is not None
        # reconciliation
        assert runtime.reconciliation_service is not None
        # startup recovery 跑过 → recovery_status 不是 multi_process_role_skip
        assert runtime.recovery_status.status != "multi_process_role_skip"

    async def test_explicit_monolith_role_builds_full_runtime(self) -> None:
        """显式 process_role="monolith" 与 None 行为一致。"""
        runtime = await build_runtime(
            _paper_settings(),
            process_role=PROCESS_ROLE_MONOLITH,
        )
        assert runtime.feature_engine is not None
        assert runtime.decision_engine is not None
        assert runtime.order_manager is not None
        assert runtime.portfolio_service is not None
        assert runtime.reconciliation_service is not None

    # ── gateway role：只装 shared ─────────────────────────────────

    async def test_gateway_role_only_builds_shared_slice(self) -> None:
        """gateway role 下：market / decision / execution / portfolio /
        reconciliation 全部为 None；shared 字段（bus、market_gateway 等）仍非 None。"""
        runtime = await build_runtime(
            _paper_settings(),
            process_role=PROCESS_ROLE_GATEWAY,
        )
        # shared 必须仍非 None
        assert runtime.bus is not None
        assert runtime.market_gateway is not None
        assert runtime.kill_switch is not None
        assert runtime.account_service is not None
        assert runtime.health_service is not None
        assert runtime.fee_resolver is not None
        # 其余 slice 必须全为 None
        assert runtime.feature_engine is None
        assert runtime.ai_service is None
        assert runtime.decision_engine is None
        assert runtime.decision_trigger is None
        assert runtime.risk_engine is None
        assert runtime.policy_engine is None
        assert runtime.execution_planner is None
        assert runtime.order_manager is None
        assert runtime.portfolio_service is None
        assert runtime.reconciliation_service is None
        # startup recovery 跳过 → 占位 RecoveryStatus
        assert runtime.recovery_status.status == "multi_process_role_skip"

    # ── market role：shared + market ─────────────────────────────

    async def test_market_role_only_builds_shared_and_market_slice(self) -> None:
        runtime = await build_runtime(
            _paper_settings(),
            process_role=PROCESS_ROLE_MARKET,
        )
        # shared
        assert runtime.bus is not None
        assert runtime.market_gateway is not None
        # market 装上
        assert runtime.feature_engine is not None
        # decision / execution / portfolio / reconciliation 全部 None
        assert runtime.ai_service is None
        assert runtime.decision_engine is None
        assert runtime.risk_engine is None
        assert runtime.order_manager is None
        assert runtime.portfolio_service is None
        assert runtime.reconciliation_service is None
        # startup recovery 跳过
        assert runtime.recovery_status.status == "multi_process_role_skip"

    # ── decision role：shared + decision ─────────────────────────

    async def test_decision_role_builds_shared_and_decision_slice(self) -> None:
        runtime = await build_runtime(
            _paper_settings(),
            process_role=PROCESS_ROLE_DECISION,
        )
        # shared
        assert runtime.bus is not None
        assert runtime.market_gateway is not None
        # decision 装上
        assert runtime.ai_service is not None
        assert runtime.decision_engine is not None
        assert runtime.decision_trigger is not None
        assert runtime.risk_engine is not None
        assert runtime.policy_engine is not None
        assert runtime.execution_planner is not None
        # market / execution / portfolio / reconciliation 全部 None
        assert runtime.feature_engine is None
        assert runtime.order_manager is None
        assert runtime.portfolio_service is None
        assert runtime.reconciliation_service is None
        # startup recovery 跳过
        assert runtime.recovery_status.status == "multi_process_role_skip"

    # ── execution role：shared + execution + portfolio + reconciliation ──

    async def test_execution_role_builds_shared_execution_portfolio_reconciliation(
        self,
    ) -> None:
        runtime = await build_runtime(
            _paper_settings(),
            process_role=PROCESS_ROLE_EXECUTION,
        )
        # shared
        assert runtime.bus is not None
        assert runtime.market_gateway is not None
        assert runtime.execution_adapter is not None
        # execution
        assert runtime.order_manager is not None
        # portfolio
        assert runtime.portfolio_service is not None
        # reconciliation
        assert runtime.reconciliation_service is not None
        # market / decision 必须为 None
        assert runtime.feature_engine is None
        assert runtime.ai_service is None
        assert runtime.decision_engine is None
        assert runtime.risk_engine is None
        assert runtime.policy_engine is None
        # execution role 是少数会跑 startup recovery 的非 monolith role
        assert runtime.recovery_status.status != "multi_process_role_skip"


# ─────────────────────────────────────────────────────────────────────
# 第 3 层：fail-fast 边界 — derivatives+hedge 在 execution-only role 下
# ─────────────────────────────────────────────────────────────────────


class TestBuildRuntimeFailFastEdgeCases(unittest.IsolatedAsyncioTestCase):
    """edge case：execution slice 在 derivatives+hedge 模式下反向依赖
    decision slice 的 risk_engine.evaluate_leg_order 作为 leg_risk_evaluator。
    Stage 3 只走 in-process bus，没有 NATS 跨进程广播，所以 execution-only
    role 启动 derivatives+hedge 必须明确报错而不是静默退化。
    """

    async def test_execution_role_with_derivatives_hedge_raises(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "execution_slice_requires_decision_slice_for_derivatives_hedge_mode",
        ):
            await build_runtime(
                _paper_settings(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    derivatives_position_mode="hedge",
                    default_symbol="BTC-USDT-SWAP",
                    allowed_symbols=("BTC-USDT-SWAP",),
                ),
                process_role=PROCESS_ROLE_EXECUTION,
            )

    async def test_monolith_with_derivatives_hedge_works(self) -> None:
        """同样的 derivatives+hedge 配置在 monolith 下应当正常装配 ——
        因为 decision slice 和 execution slice 在同一个进程里，
        risk_engine 在内存中可见。"""
        runtime = await build_runtime(
            _paper_settings(
                trading_product_type="derivatives",
                margin_mode="cross",
                derivatives_position_mode="hedge",
                default_symbol="BTC-USDT-SWAP",
                allowed_symbols=("BTC-USDT-SWAP",),
            ),
            process_role=PROCESS_ROLE_MONOLITH,
        )
        assert runtime.risk_engine is not None
        assert runtime.order_manager is not None


if __name__ == "__main__":
    unittest.main()
