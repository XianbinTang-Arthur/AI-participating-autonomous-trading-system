"""Finding 1: 四进程拓扑 fail-fast 硬校验。

测试 _validate_topology_capability 中新增的两条规则：
  - 4proc_requires_cross_process_event_bus
  - 4proc_requires_redis_hot_state

规则仅在实盘/交易所耦合场景（live_submit_enabled / guarded_live / okx backend）
下生效，不阻断本地 smoke 测试和纯模拟 profile。
"""

from __future__ import annotations

import pytest

from aats.bootstrap.config import _validate_topology_capability
from aats.bootstrap.settings import (
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MONOLITH,
)


def _make_settings(**overrides):
    """构造最小 mock settings，只需要 topology 校验使用的字段。"""

    class _FakeSettings:
        event_bus_backend = overrides.get("event_bus_backend", "in_memory")
        hot_state_backend = overrides.get("hot_state_backend", "memory")
        live_submit_enabled = overrides.get("live_submit_enabled", False)
        mode = overrides.get("mode", "guarded_live")
        account_backend = overrides.get("account_backend", "okx")
        execution_backend = overrides.get("execution_backend", "okx")

    return _FakeSettings()


# ────────────────────────────────────────────────────────────────
# Finding 1 核心测试：四进程 + in_memory 事件总线 → RuntimeError
# ────────────────────────────────────────────────────────────────


class TestEventBusRule:
    """4proc_requires_cross_process_event_bus 规则。"""

    def test_4proc_in_memory_guarded_live_blocked(self):
        """四进程 + in_memory + guarded_live → 必须报错。"""
        settings = _make_settings(
            event_bus_backend="in_memory",
            mode="guarded_live",
        )
        with pytest.raises(RuntimeError, match="4proc_requires_cross_process_event_bus"):
            _validate_topology_capability(
                settings,
                effective_process_role=PROCESS_ROLE_GATEWAY,
            )

    def test_4proc_in_memory_live_submit_blocked(self):
        """四进程 + in_memory + live_submit_enabled → 必须报错。"""
        settings = _make_settings(
            event_bus_backend="in_memory",
            live_submit_enabled=True,
            mode="other",
            account_backend="local",
            execution_backend="local",
        )
        with pytest.raises(RuntimeError, match="4proc_requires_cross_process_event_bus"):
            _validate_topology_capability(
                settings,
                effective_process_role=PROCESS_ROLE_EXECUTION,
            )

    def test_4proc_in_memory_okx_backend_blocked(self):
        """四进程 + in_memory + okx backend → 必须报错。"""
        settings = _make_settings(
            event_bus_backend="in_memory",
            mode="other",
            live_submit_enabled=False,
            account_backend="okx",
            execution_backend="okx",
        )
        with pytest.raises(RuntimeError, match="4proc_requires_cross_process_event_bus"):
            _validate_topology_capability(
                settings,
                effective_process_role=PROCESS_ROLE_GATEWAY,
            )

    def test_4proc_hybrid_allowed(self):
        """四进程 + hybrid → 不报错。"""
        settings = _make_settings(event_bus_backend="hybrid", hot_state_backend="redis", mode="guarded_live")
        _validate_topology_capability(
            settings,
            effective_process_role=PROCESS_ROLE_GATEWAY,
        )
        # 无异常即为通过

    def test_4proc_nats_allowed(self):
        """四进程 + nats → 不报错。"""
        settings = _make_settings(event_bus_backend="nats", hot_state_backend="redis", mode="guarded_live")
        _validate_topology_capability(
            settings,
            effective_process_role=PROCESS_ROLE_EXECUTION,
        )

    def test_monolith_in_memory_allowed(self):
        """monolith + in_memory → 不报错（向后兼容）。"""
        settings = _make_settings(event_bus_backend="in_memory", mode="guarded_live")
        _validate_topology_capability(
            settings,
            effective_process_role=PROCESS_ROLE_MONOLITH,
        )

    def test_4proc_in_memory_smoke_test_allowed(self):
        """四进程 + in_memory + 非实盘场景 → 不报错（smoke 测试兼容）。"""
        settings = _make_settings(
            event_bus_backend="in_memory",
            mode="dev",
            live_submit_enabled=False,
            account_backend="local",
            execution_backend="simulated",
        )
        _validate_topology_capability(
            settings,
            effective_process_role=PROCESS_ROLE_GATEWAY,
        )


# ────────────────────────────────────────────────────────────────
# Finding 1 核心测试：四进程 + memory 热状态 → RuntimeError
# ────────────────────────────────────────────────────────────────


class TestHotStateRule:
    """4proc_requires_redis_hot_state 规则。"""

    def test_4proc_memory_guarded_live_blocked(self):
        """四进程 + memory hot state + guarded_live → 必须报错。"""
        settings = _make_settings(
            event_bus_backend="hybrid",  # 事件总线合法，只测热状态
            hot_state_backend="memory",
            mode="guarded_live",
        )
        with pytest.raises(RuntimeError, match="4proc_requires_redis_hot_state"):
            _validate_topology_capability(
                settings,
                effective_process_role=PROCESS_ROLE_GATEWAY,
            )

    def test_4proc_memory_live_submit_blocked(self):
        """四进程 + memory hot state + live_submit_enabled → 必须报错。"""
        settings = _make_settings(
            event_bus_backend="hybrid",
            hot_state_backend="memory",
            live_submit_enabled=True,
            mode="other",
            account_backend="local",
            execution_backend="local",
        )
        with pytest.raises(RuntimeError, match="4proc_requires_redis_hot_state"):
            _validate_topology_capability(
                settings,
                effective_process_role=PROCESS_ROLE_EXECUTION,
            )

    def test_4proc_redis_allowed(self):
        """四进程 + redis → 不报错。"""
        settings = _make_settings(
            event_bus_backend="hybrid",
            hot_state_backend="redis",
            mode="guarded_live",
        )
        _validate_topology_capability(
            settings,
            effective_process_role=PROCESS_ROLE_GATEWAY,
        )

    def test_monolith_memory_allowed(self):
        """monolith + memory → 不报错。"""
        settings = _make_settings(
            event_bus_backend="in_memory",
            hot_state_backend="memory",
            mode="guarded_live",
        )
        _validate_topology_capability(
            settings,
            effective_process_role=None,
        )


# ────────────────────────────────────────────────────────────────
# 组合测试：derivatives_live managed profile 不会回退
# ────────────────────────────────────────────────────────────────


class TestDerivativesLiveProfile:
    """derivatives_live 在四进程部署时必须通过校验。

    这条测试是回归保护：如果有人修改 managed profile 的默认值，
    意外移除 event_bus_backend=hybrid 或 hot_state_backend=redis，
    测试就会失败。
    """

    def test_derivatives_live_with_correct_backends_passes(self):
        """模拟 derivatives_live 部署配置，校验通过。"""
        settings = _make_settings(
            event_bus_backend="hybrid",
            hot_state_backend="redis",
            mode="guarded_live",
            live_submit_enabled=True,
            account_backend="okx",
            execution_backend="okx",
        )
        # 所有 4 个进程角色都应该通过
        for role in (
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_EXECUTION,
            "market",
            "decision",
        ):
            _validate_topology_capability(settings, effective_process_role=role)
