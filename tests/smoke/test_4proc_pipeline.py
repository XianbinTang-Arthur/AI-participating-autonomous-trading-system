"""Stage 5 smoke 测试：4 进程拓扑端到端联动 (子任务 5e)。

设计动机：
* tests/unit/test_build_runtime_slice_gating.py 已经覆盖了「单个 role 单独
  build_runtime」的契约。但 4 进程拓扑下真正的风险是「4 个 runtime 同时存在
  且通过同一份 EventBus 互相通信」时的问题——例如某个 slice 偷偷依赖了一个
  全局 singleton、或者 process_role 派生的 advisory lock_key 在并发场景下
  撞键。
* 这里用同一个 Python 进程同时构造 4 份 runtime（每份用自己的 in-memory
  bus + memory storage），验证：
  1) 4 份 runtime 能并发构造、各自 start_background_tasks、再各自干净 stop；
  2) 每份 runtime 上的 slice 字段满足 process_role 矩阵约束；
  3) 在干净的 finally 路径下，stop_background_tasks 不会留下 pending task；
  4) market 进程的 in-memory bus 能 publish 一份 market snapshot，本进程的
     subscriber 能收到——这是 EventBus 抽象的最小契约，跨进程版本由
     tests/integration/test_nats_event_bus_roundtrip.py 在有 NATS 时覆盖。

为什么是 smoke 而不是 integration：
* 这里不拉 docker、不连真 Postgres、不连真 NATS——纯 in-memory 闭环；
* 但比 unit 更接近真实路径：跑完整的 build_runtime → start → publish/sub →
  stop 全链路，能在没有外部依赖的前提下捕获 boot/teardown 的回归。

WSL2 端到端验证（拉真 docker compose 跑 4 进程）见 README 与 RUNBOOK，
不在 pytest 范围内——它需要 ~30s 启动和真实云依赖，单独走人工/CI nightly。
"""
from __future__ import annotations

import asyncio
import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import (
    AATSSettings,
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
)


def _smoke_settings(**overrides: object) -> AATSSettings:
    """构造 paper_live + memory storage 的最小可启动配置。

    与 test_build_runtime_slice_gating._paper_settings 一致，但单独定义在
    smoke 包里以避免单元测试 helper 在 smoke 测试运行时的 import 顺序耦合。
    """
    base: dict[str, object] = {
        "mode": "paper_live",
        "market_data_backend": "demo",
        "execution_backend": "paper",
        "account_backend": "disabled",
        "account_read_enabled": False,
        "storage_mode": "memory",
        "event_persistence_mode": "strict",
        "event_bus_backend": "in_memory",
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


# ─────────────────────────────────────────────────────────────────────
# 1) 4 个 role 同进程并发 build / start / stop
# ─────────────────────────────────────────────────────────────────────


class TestFourProcessTopologySmokeBoot(unittest.IsolatedAsyncioTestCase):
    """4 份 runtime 在同一个事件循环里同时跑完整生命周期。

    这是 4 进程拓扑的「能不能同时活下去」smoke 测试。任何一个 role 在同进程
    并发场景下出现 import-time 副作用、全局 singleton 抢占、或资源没有清理
    干净的问题，这里都会立刻炸开。
    """

    async def test_four_runtimes_build_concurrently_and_stop_cleanly(self) -> None:
        roles = (
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_MARKET,
            PROCESS_ROLE_DECISION,
            PROCESS_ROLE_EXECUTION,
        )

        # 并发构造 4 份 runtime（asyncio.gather 让 4 个 build_runtime 在
        # 同一个 loop 上交替执行，这是 4 进程拓扑下最容易撞 race 的形态）。
        runtimes = await asyncio.gather(
            *(build_runtime(_smoke_settings(), process_role=role) for role in roles)
        )
        self.assertEqual(len(runtimes), 4)

        try:
            # 4 份 runtime 同时 start_background_tasks
            await asyncio.gather(*(rt.start_background_tasks() for rt in runtimes))

            # 验证每份 runtime 的 background_tasks 列表是独立的（不是共享 list）
            background_task_lists = [rt.background_tasks for rt in runtimes]
            for idx_a in range(len(background_task_lists)):
                for idx_b in range(idx_a + 1, len(background_task_lists)):
                    self.assertIsNot(
                        background_task_lists[idx_a],
                        background_task_lists[idx_b],
                        "4 份 runtime 的 background_tasks 列表必须各自独立，不能共享",
                    )

            # 让所有 runtime 至少有一个 event loop tick 的机会跑后台任务
            await asyncio.sleep(0)
        finally:
            # 关键：必须保证所有 runtime 都能干净停掉，否则会泄露后台 task
            await asyncio.gather(
                *(rt.stop_background_tasks() for rt in runtimes),
                return_exceptions=False,
            )

        # stop 之后所有 background_tasks 列表都应当被清空
        for rt in runtimes:
            self.assertEqual(
                len(rt.background_tasks),
                0,
                "stop_background_tasks 之后 runtime.background_tasks 必须为空，否则后续 GC 拿不到引用",
            )

    async def test_each_role_runtime_has_expected_slice_population(self) -> None:
        """4 份 runtime 的 slice 矩阵必须按 process_role 区分开。

        这是「能 build 出来」之上的进一步约束：build 成功不代表装对了 slice。
        如果某个 role 偷偷把不该装的 slice 装上了，跨进程拓扑下就会出现两个
        进程同时改一份状态的问题。
        """
        runtimes_by_role: dict[str, object] = {}
        roles = (
            PROCESS_ROLE_GATEWAY,
            PROCESS_ROLE_MARKET,
            PROCESS_ROLE_DECISION,
            PROCESS_ROLE_EXECUTION,
        )
        try:
            for role in roles:
                runtimes_by_role[role] = await build_runtime(
                    _smoke_settings(), process_role=role
                )

            gateway = runtimes_by_role[PROCESS_ROLE_GATEWAY]
            market = runtimes_by_role[PROCESS_ROLE_MARKET]
            decision = runtimes_by_role[PROCESS_ROLE_DECISION]
            execution = runtimes_by_role[PROCESS_ROLE_EXECUTION]

            # gateway：只装 shared slice
            self.assertIsNone(gateway.feature_engine, "gateway 不应当装 market slice")
            self.assertIsNone(gateway.decision_engine, "gateway 不应当装 decision slice")
            self.assertIsNone(gateway.order_manager, "gateway 不应当装 execution slice")
            self.assertIsNone(gateway.portfolio_service, "gateway 不应当装 portfolio slice")
            self.assertIsNone(gateway.reconciliation_service, "gateway 不应当装 reconciliation slice")

            # market：shared + market
            self.assertIsNotNone(market.feature_engine, "market 必须装 feature_engine")
            self.assertIsNone(market.decision_engine, "market 不应当装 decision slice")
            self.assertIsNone(market.order_manager, "market 不应当装 execution slice")

            # decision：shared + decision（不含 market 的 feature_engine）
            self.assertIsNotNone(decision.decision_engine, "decision 必须装 decision_engine")
            self.assertIsNotNone(decision.risk_engine, "decision 必须装 risk_engine")
            self.assertIsNotNone(decision.execution_planner, "decision 必须装 execution_planner")
            self.assertIsNone(decision.feature_engine, "decision 不应当装 market slice")
            self.assertIsNone(decision.order_manager, "decision 不应当装 execution slice")
            self.assertIsNone(decision.portfolio_service, "decision 不应当装 portfolio slice")

            # execution：shared + execution + portfolio + reconciliation
            self.assertIsNotNone(execution.order_manager, "execution 必须装 order_manager")
            self.assertIsNotNone(execution.portfolio_service, "execution 必须装 portfolio_service")
            self.assertIsNotNone(execution.reconciliation_service, "execution 必须装 reconciliation_service")
            self.assertIsNone(execution.feature_engine, "execution 不应当装 market slice")
            self.assertIsNone(execution.decision_engine, "execution 不应当装 decision slice")

            # 4 个 runtime 的 bus 实例必须各自独立（不能不小心共享一个全局 bus）
            buses = [runtimes_by_role[r].bus for r in roles]
            for idx_a in range(len(buses)):
                for idx_b in range(idx_a + 1, len(buses)):
                    self.assertIsNot(
                        buses[idx_a],
                        buses[idx_b],
                        f"{roles[idx_a]} 与 {roles[idx_b]} 的 EventBus 必须各自独立——"
                        "in-memory bus 不应当被全局缓存",
                    )
        finally:
            for rt in runtimes_by_role.values():
                await rt.stop_background_tasks()


# ─────────────────────────────────────────────────────────────────────
# 2) market role 的 EventBus 最小 publish/subscribe 闭环
# ─────────────────────────────────────────────────────────────────────


class TestMarketSliceBusRoundtripSmoke(unittest.IsolatedAsyncioTestCase):
    """单进程内 market role 的 in-memory bus 必须满足 publish → subscribe 契约。

    跨进程版本（NATS JetStream + 真容器）由 tests/integration/test_nats_event_bus_roundtrip
    在 AATS_RUN_NATS_INTEGRATION=1 且本地 docker 可用时覆盖。这里只验证
    in-memory bus 的最小闭环——足够发现「subscribe 没绑到正确 topic」「publish
    payload 序列化路径回归」这一类问题。
    """

    async def test_market_role_inmemory_bus_publish_subscribe_roundtrip(self) -> None:
        runtime = await build_runtime(
            _smoke_settings(),
            process_role=PROCESS_ROLE_MARKET,
        )
        try:
            received: list[dict] = []

            # InMemoryEventBus.subscribe 的 handler 签名是 Callable[[dict], Awaitable[None]]
            # 接收的是 {"topic", "key", "payload"} 字典——见 aats/bus/memory_bus.py
            async def _handler(message: dict) -> None:
                received.append(message)

            test_topic = "smoke.market.publish_subscribe"
            await runtime.bus.subscribe(test_topic, _handler)

            from aats.schemas.common import EventEnvelope

            envelope = EventEnvelope(
                event_type="smoke.market.publish_subscribe",
                topic=test_topic,
                key="smoke",
                payload={"hello": "smoke"},
                source_component="test_4proc_pipeline",
            )
            # persist=False 跳过 event_store.append，避免 smoke 测试要求一个
            # 配好的 event store；契约层只关心 publish → subscriber 能闭环。
            await runtime.bus.publish_envelope(envelope, persist=False)

            # in-memory bus 是同步触发的，但保留一次 await 让任何 task 排队点
            # 都能收尾。
            await asyncio.sleep(0)

            self.assertEqual(len(received), 1, "subscriber 必须收到 publish 的事件")
            self.assertEqual(received[0]["topic"], test_topic)
            self.assertEqual(received[0]["key"], "smoke")
            self.assertEqual(received[0]["payload"]["payload"], {"hello": "smoke"})
        finally:
            await runtime.stop_background_tasks()


if __name__ == "__main__":
    unittest.main()
