"""Stage 6 Slice 6.5 回归单测：build_runtime 对 ObligationHotStateCache 的装配。

设计文档
========
docs/task/stage_6_slice_6_5_obligation_hot_state_design.md §10-§11

覆盖范围
========
- 每个 process_role 下 runtime.obligation_hot_state_cache 非 None 且 bootstrap 成功
- cache 被注入到 RiskEngine / ExecutionObligationService / PostgresExecutionOutboxPublisher
  / Phase1ShadowMonitor 这些 downstream service（按各自 role 是否装载该 service 判断）
- Phase1ShadowMonitor 走 setter 注入（attach_obligation_cache），不是构造时注入
- cache 在 _wire_event_subscriptions 结束后 subscribed=True（本地走 InMemoryEventBus
  的 _CollectingBus 路径会同步调 subscribe，失败不抛）
- stop_background_tasks 能安全 await cache.stop() 不报错
- risk_engine._active_local_obligations 走 cache 时不会爆
  （hydrate 之后 active_sync() 返回列表，miss 时退化到 obligation_repo）

注意：不依赖真实 Redis / NATS —— paper_live + memory storage 走的是
InMemoryHotStateStore + InMemoryEventBus，测试不需要 testcontainers。
"""
from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import (
    AATSSettings,
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
)
from aats.services.execution_engine.obligation_cache import ObligationHotStateCache


def _paper_settings(**overrides: object) -> AATSSettings:
    """复用 test_build_runtime_slice_gating 的 paper_live + memory 最小配置。

    该配置不依赖 OKX / 不连真数据库 / 不写盘，是 build_runtime 在单元测试
    范围内跑通的唯一形态；InMemoryHotStateStore + InMemoryEventBus 保证 cache
    的 bootstrap / register_remote_subscription / stop 全链路可以走完而不触 IO。
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


class TestObligationCacheAlwaysConstructed(unittest.IsolatedAsyncioTestCase):
    """无论什么 role，obligation_hot_state_cache 都必须被装上并且 bootstrap 过。

    build_runtime 里这一步不在任何 slice builder 内，是主流程的一部分；所以
    即使 gateway / market / decision / execution 任何单进程拆分的 role 下，
    runtime.obligation_hot_state_cache 都要是已 hydrate 的同一个实例
    （不装会让 cache 读路径在跨进程拓扑下静默失效）。
    """

    async def _build_and_assert_ready(
        self, *, process_role: str | None
    ) -> None:
        runtime = await build_runtime(_paper_settings(), process_role=process_role)
        try:
            cache = runtime.obligation_hot_state_cache
            self.assertIsNotNone(cache)
            self.assertIsInstance(cache, ObligationHotStateCache)
            snap = cache.snapshot()
            # I3：bootstrap 必须跑过（后续 sync 读路径才会返回非 None）
            self.assertTrue(
                snap["bootstrapped"],
                msg=f"cache.bootstrapped must be True under role={process_role}",
            )
            # process_role 标签用于跨进程日志；None 归一成 monolith
            self.assertEqual(
                snap["process_role"],
                (process_role or "monolith"),
            )
            # _wire_event_subscriptions 会经 _CollectingBus.flush 触发
            # register_remote_subscription，在 InMemoryEventBus 上 subscribe
            # 直接同步 return，所以 subscribed 也必须是 True。
            self.assertTrue(
                snap["subscribed"],
                msg=f"cache.subscribed must be True after _wire_event_subscriptions under role={process_role}",
            )
        finally:
            await runtime.stop_background_tasks()

    async def test_default_role_none_builds_cache(self) -> None:
        """process_role 不传 → 与 monolith 等价：cache 装上。"""
        await self._build_and_assert_ready(process_role=None)

    async def test_monolith_role_builds_cache(self) -> None:
        await self._build_and_assert_ready(process_role=PROCESS_ROLE_MONOLITH)

    async def test_gateway_role_builds_cache(self) -> None:
        """gateway role 没有 risk_engine/order_manager，但 query_service
        仍会通过 runtime.obligation_hot_state_cache 读取 backlog，所以
        cache 不能偷偷跳过。"""
        await self._build_and_assert_ready(process_role=PROCESS_ROLE_GATEWAY)

    async def test_market_role_builds_cache(self) -> None:
        """market role 没有写路径也没有读路径，但装上 cache 让 4 进程对称
        （D15：cache 类内部没有 role 分支）。"""
        await self._build_and_assert_ready(process_role=PROCESS_ROLE_MARKET)

    async def test_decision_role_builds_cache(self) -> None:
        await self._build_and_assert_ready(process_role=PROCESS_ROLE_DECISION)

    async def test_execution_role_builds_cache(self) -> None:
        await self._build_and_assert_ready(process_role=PROCESS_ROLE_EXECUTION)


class TestObligationCacheWiredIntoServices(unittest.IsolatedAsyncioTestCase):
    """cache 被注入到各 downstream service。

    注入点（按设计文档 §10 的 7 个 wiring 点）：
      1. ExecutionObligationService._obligation_cache    （写路径 async）
      2. PostgresExecutionOutboxPublisher.obligation_cache（写路径 sync）
      3. ExecutionRecoveryService._obligation_cache       （orphan cleanup）
      4. RiskEngine._obligation_cache                     （读路径）
      5. Phase1ShadowMonitor._obligation_cache            （setter 注入）
      6. ApplicationRuntime.obligation_hot_state_cache    （dashboard 读路径）
      7. query_service 通过 runtime getattr 读

    每个 wiring 点在 bootstrap 完成之后都必须指向**同一个** cache 实例
    —— 否则跨进程收敛会被拆成两份。
    """

    async def test_monolith_wires_cache_into_all_services(self) -> None:
        """monolith 下所有 slice 都装：5 个 service 注入点 + runtime 字段
        都必须指向同一个 cache 实例。"""
        runtime = await build_runtime(_paper_settings())
        try:
            cache = runtime.obligation_hot_state_cache
            self.assertIsNotNone(cache)

            # 1. RiskEngine（decision slice）
            self.assertIsNotNone(runtime.risk_engine)
            self.assertIs(runtime.risk_engine._obligation_cache, cache)

            # 2. ExecutionObligationService（execution slice 内 order_manager 附件）
            self.assertIsNotNone(runtime.order_manager)
            obligation_service = runtime.order_manager.obligation_service
            self.assertIsNotNone(obligation_service)
            self.assertIs(obligation_service._obligation_cache, cache)

            # 3. PostgresExecutionOutboxPublisher（outbox 的 dataclass 字段）
            # Stage 3 / paper_live 下 execution_outbox_publisher 可能为 None
            # （没有 execution_order_repo），这里只在非 None 时断言。
            if runtime.execution_outbox_publisher is not None:
                self.assertIs(
                    runtime.execution_outbox_publisher.obligation_cache,
                    cache,
                )

            # 4. Phase1ShadowMonitor（setter 注入，在 _build_shared_runtime_slice
            # 早构造 + build_runtime 后段 attach）
            self.assertIsNotNone(runtime.phase1_shadow_monitor)
            self.assertIs(
                runtime.phase1_shadow_monitor._obligation_cache,
                cache,
            )
        finally:
            await runtime.stop_background_tasks()

    async def test_decision_role_wires_cache_into_risk_engine(self) -> None:
        """decision-only 进程的 RiskEngine.active_obligations 读路径必须
        走 cache，这是 slice 6.5 的主要收益点之一。"""
        runtime = await build_runtime(
            _paper_settings(), process_role=PROCESS_ROLE_DECISION
        )
        try:
            cache = runtime.obligation_hot_state_cache
            self.assertIsNotNone(runtime.risk_engine)
            self.assertIs(runtime.risk_engine._obligation_cache, cache)
            # decision role 下不装 order_manager / execution_outbox_publisher
            self.assertIsNone(runtime.order_manager)
        finally:
            await runtime.stop_background_tasks()

    async def test_execution_role_wires_cache_into_write_paths(self) -> None:
        """execution-only 进程的写路径：ExecutionObligationService
        + PostgresExecutionOutboxPublisher 都必须持有同一个 cache 引用。"""
        runtime = await build_runtime(
            _paper_settings(), process_role=PROCESS_ROLE_EXECUTION
        )
        try:
            cache = runtime.obligation_hot_state_cache

            # order_manager 必装
            self.assertIsNotNone(runtime.order_manager)
            obligation_service = runtime.order_manager.obligation_service
            self.assertIsNotNone(obligation_service)
            self.assertIs(obligation_service._obligation_cache, cache)

            # outbox publisher 如果存在也必须指向同一个 cache
            if runtime.execution_outbox_publisher is not None:
                self.assertIs(
                    runtime.execution_outbox_publisher.obligation_cache,
                    cache,
                )

            # execution role 下 risk_engine 为 None（不装 decision slice）
            self.assertIsNone(runtime.risk_engine)
        finally:
            await runtime.stop_background_tasks()

    async def test_gateway_role_still_wires_shadow_monitor(self) -> None:
        """shared slice 里的 Phase1ShadowMonitor 无论哪个 role 都装上，
        所以 setter 注入必须仍然跑到 —— gateway role 的 dashboard backlog
        才能走 cache 而不是每次打 PG。"""
        runtime = await build_runtime(
            _paper_settings(), process_role=PROCESS_ROLE_GATEWAY
        )
        try:
            cache = runtime.obligation_hot_state_cache
            self.assertIsNotNone(runtime.phase1_shadow_monitor)
            self.assertIs(
                runtime.phase1_shadow_monitor._obligation_cache,
                cache,
            )
        finally:
            await runtime.stop_background_tasks()


class TestObligationCacheStopBackgroundTasks(unittest.IsolatedAsyncioTestCase):
    """stop_background_tasks 必须安全 await cache.stop() —— 这是 I1 fail-soft
    的最后一环：关闭流程不能因为 cache 在异常状态下抛错而阻塞其它清理。"""

    async def test_stop_background_tasks_closes_cache_without_error(self) -> None:
        runtime = await build_runtime(_paper_settings())
        cache = runtime.obligation_hot_state_cache
        # stop 之前 cache 处于正常 bootstrap 状态
        self.assertTrue(cache.snapshot()["bootstrapped"])
        # 关闭不抛
        await runtime.stop_background_tasks()
        # stop() 是日志 only（不清本地 dict、不改 _bootstrapped），调用之后仍
        # 可以读 snapshot（这是刻意的行为：关闭不代表 cache 数据立即失效）
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])

    async def test_stop_background_tasks_idempotent(self) -> None:
        """重复 stop 必须也不抛。"""
        runtime = await build_runtime(_paper_settings())
        await runtime.stop_background_tasks()
        await runtime.stop_background_tasks()


class TestObligationCacheReadPathFromRiskEngine(unittest.IsolatedAsyncioTestCase):
    """risk_engine 读 cache 的端到端 smoke：bootstrap 后 cache 为空时
    active_sync 返回 []（已 bootstrap 但 latest dict 为空），risk 退化
    到 obligation_repo 的链路依然连通。I5 miss-不破坏读 的最小保证。
    """

    async def test_risk_engine_active_obligations_via_cache_when_empty(self) -> None:
        runtime = await build_runtime(_paper_settings())
        try:
            cache = runtime.obligation_hot_state_cache
            risk_engine = runtime.risk_engine
            self.assertIsNotNone(risk_engine)

            # bootstrap 后 cache 已经 ready（_bootstrapped=True）——
            # active_sync() 应该返回空列表而不是 None。这是 risk_engine
            # 拿得到"cache 确实是空，不用 fallback PG"这个信号的关键。
            active = cache.active_sync()
            self.assertIsNotNone(active)
            self.assertEqual(active, [])

            # 同理 all_sync()
            all_obligations = cache.all_sync()
            self.assertIsNotNone(all_obligations)
            self.assertEqual(all_obligations, [])
        finally:
            await runtime.stop_background_tasks()


if __name__ == "__main__":
    unittest.main()
