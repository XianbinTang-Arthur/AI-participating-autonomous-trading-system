"""NATS JetStream EventBus 骨架（Stage 4 代码准备）。

本模块仅提供：
- NatsEventBus：兼容 EventBus 接口的 JetStream 客户端封装
- HybridEventBus：把"关键 topic"路由到 NATS、把"观察者 topic"留在内存

⚠️ 本模块不会在 build_runtime 中被自动启用；它是 Stage 4 实盘多进程化的
代码基座。集成到 build_runtime 的工作放在 Stage 4 的迁移阶段进行，并需要
跑通 docker-compose 启动 NATS、配置 JetStream stream 之后再切换。

⚠️ nats-py 是可选依赖：本模块只在实例化 NatsEventBus 时才 import nats，
不会让 monolith 模式必须装 nats-py。

设计要点：
1. 持久化语义：critical topic（决策/执行/风险事件）走 JetStream 文件存储，
   保证至少一次投递；observer topic（仪表盘 / 指标）走内存，避免落盘开销。
2. Subject 命名：所有 topic 都加 `aats.` 前缀，避免和其他 NATS 用户冲突。
3. Durable consumer：subscribe 时用 `consumer_name` 作为 durable name，
   重启后从未确认位置继续消费，保证 exactly-once-effective。
4. 序列化：复用现有 EventEnvelope.model_dump_json()，跨进程仍走标准 schema。
5. 反压：JetStream 自身有 max_ack_pending 限制；handler 失败不会丢消息，
   会按 ack_wait 后重新投递（最多 max_deliver 次）。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus, MessageHandler
from aats.events import topics as _topics
from aats.schemas.common import EventEnvelope
from aats.storage.base import EventStore

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from nats.aio.client import Client as NATSClient
    from nats.js import JetStreamContext


# ─────────────────────────────────────────────────────────────────────
# Topic 路由策略
#
# ⚠️ 5c 修复说明：之前这两个 frozenset 用的是手写字面量名（如 "execution_intents"），
# 与 aats/events/topics.py 实际使用的 dotted name（如 "execution.order_intents"）
# 完全不匹配。Stage 4 集成测试通过的唯一原因：HybridBusRouting.default_route 是
# "critical"，所有未匹配的 topic 都 fallback 到 NATS。一旦未来某个 topic 被错配
# 到 observer 集合，就会立刻丢消息。
#
# 修复策略：
#   1) 全部 topic 名改为引用 `aats.events.topics` 模块的常量，编译期保证正确
#   2) 把 HybridBusRouting 的默认 default_route 改为 None，未知 topic 抛
#      UnroutedTopicError，强制开发者显式归类
#   3) test_all_topics_module_constants_are_routed 单测枚举 topics.py 全部常量
#      验证每条都被归类（防止未来加新 topic 漏配）
#
# 归类标准：
#   critical = 丢失会导致状态不一致 / 资金安全 / 决策饿死 / 合规追溯断链
#   observer = 纯监控、分析报告、可视化指标，丢失只影响可观测性
# ─────────────────────────────────────────────────────────────────────

# 关键 topic：决策/执行/风险/对账事件，必须持久化到 JetStream，跨进程消费
DEFAULT_CRITICAL_TOPICS: frozenset[str] = frozenset(
    {
        # ── 行情 / 特征（决策饿死保护）─────────────────────
        _topics.MARKET_SNAPSHOTS,         # 决策直接依赖；订阅丢失会饿死决策
        _topics.FEATURE_SNAPSHOTS,        # 特征派生层；同上
        _topics.ACCOUNT_BASELINES,        # 资金/仓位基线；决策依赖
        # ── 决策路径中间产物 / 决策结果 ─────────────────────
        _topics.DECISION_CONTEXTS,        # 决策上下文；决策路径核心
        _topics.BASELINE_ASSESSMENTS,     # 基线评估；决策路径
        _topics.AI_ASSESSMENTS,           # AI 评估；决策路径
        _topics.AI_DECISION_BRIEFS,       # AI 决策简报；决策核心载体
        _topics.AI_SHADOW_DECISIONS,      # 影子决策；shadow→real 切换依据
        _topics.AI_SHADOW_EVALUATIONS,    # 影子评估；同上
        _topics.AI_DEGRADATION_EVENTS,    # AI 降级事件；触发 risk 自动降级
        _topics.STRATEGY_COORDINATOR_SNAPSHOTS,  # 协调器状态；重启恢复依赖
        _topics.STRATEGY_SLEEVE_INTENTS,  # sleeve 意图；决策→执行
        _topics.PORTFOLIO_ALLOCATION_DECISIONS,  # 组合分配决策
        _topics.STRATEGY_EXECUTION_BUNDLES,      # 策略执行 bundle；决策→执行核心
        _topics.POSITION_TARGETS,         # 仓位目标；决策→执行
        _topics.OVERLAY_PARENT_EXPOSURES, # overlay 父级暴露
        _topics.DECISION_OUTCOMES,        # 决策结果；审计 + 下游
        _topics.POLICY_DECISIONS,         # policy 决策
        # ── 风险 / 执行 / 资金 ──────────────────────────────
        _topics.RISK_DECISIONS,           # 风险决策；决定执行/不执行
        _topics.EXECUTION_PLANS,          # 执行计划
        _topics.ORDER_INTENTS,            # 订单意图；执行核心
        _topics.ORDER_UPDATES,            # 订单状态机
        _topics.FILL_EVENTS,              # 成交；资金变动核心
        _topics.PORTFOLIO_BALANCE_DELTAS, # 余额变动镜像
        _topics.PORTFOLIO_SNAPSHOTS,      # 组合快照
        _topics.RECONCILIATION_REPORTS,   # 对账报告
        _topics.RECONCILIATION_VALIDATIONS,  # 对账验证
        _topics.REPLAY_VALIDATIONS,       # 回放验证；合规追溯
        # ── 审计 / operator / 错误流 ────────────────────────
        _topics.AUDIT_RECORDS,            # 审计记录；合规不能丢
        _topics.OPERATOR_ACTIONS,         # operator 人工动作驱动状态变化
        _topics.EXECUTION_ERROR_SUMMARIES,    # 执行错误汇总；驱动 risk 降级
        _topics.PROCESSING_FAILURES,      # 处理失败；同上
        # ── strategy profile 切换路径 ─────────────────────
        _topics.STRATEGY_PROFILE_RECOMMENDATIONS,    # profile 推荐
        _topics.STRATEGY_PROFILE_ACTIVATIONS,        # profile 激活；影响实盘
        _topics.STRATEGY_PROFILE_REJECTIONS,         # profile 拒绝；状态记录
        _topics.STRATEGY_PROFILE_SELECTION_DECISIONS,    # profile 选择决策
        _topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES, # 自动回滚规则配置
        _topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,    # 激活规则配置
    }
)

# 观察者 topic：仪表盘/指标流/调试事件，量大、丢失无关键影响，留在内存
DEFAULT_OBSERVER_TOPICS: frozenset[str] = frozenset(
    {
        _topics.HEALTH_SNAPSHOTS,         # 系统健康指标；纯监控
        _topics.BLOCKER_SNAPSHOTS,        # operator dashboard 阻塞展示
        _topics.AI_PERFORMANCE_REPORTS,   # AI 表现报告；纯报告
        _topics.STRATEGY_PROFILE_EVALUATIONS,         # profile 评估输入；分析层
        _topics.STRATEGY_PROFILE_COMPARISON_REPORTS,  # profile 比较报告
        _topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,# profile 优化报告
    }
)


class UnroutedTopicError(KeyError):
    """未在 critical / observer 任一集合中归类的 topic 被请求路由时抛出。

    Why: HybridBusRouting 默认 default_route=None 时，未知 topic 必须抛错而不是
    silent fallback。这避免了 Stage 4 那种 "路由表错位 + fallback 蒙混过关" 的
    隐患——一旦有人加新 topic 但忘记归类，系统会立刻在 publish/subscribe 第一次
    调用时炸响而不是默默走错路径。
    """


# ─────────────────────────────────────────────────────────────────────
# NATS 配置
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class NatsBusConfig:
    """NatsEventBus 实例化所需的配置。"""

    servers: tuple[str, ...] = ("nats://127.0.0.1:4222",)
    name: str = "aats"
    subject_prefix: str = "aats."
    stream_name: str = "AATS_EVENTS"
    # JetStream stream 内消息最大保留时间（秒），过期消息会被自动丢弃。
    # 默认 7 天足够 Stage 4 集成回放，生产可调长（30/90 天）。
    stream_max_age_seconds: float = 7 * 24 * 60 * 60
    # 单条事件最大 ack_wait（秒），handler 处理超时后会被重试
    ack_wait_seconds: float = 30.0
    # 单个消费者最多 in-flight ack 待确认数
    max_ack_pending: int = 256
    # 单条消息最大重投递次数（超出后会被丢入死信主题）
    max_deliver: int = 5
    # 连接超时
    connect_timeout_seconds: float = 5.0
    # 重连最大次数（-1 = 无限重连）
    max_reconnect_attempts: int = -1
    # 消费者持久 name 前缀（每个进程角色独立）
    durable_name_prefix: str = "aats-"

    def subject_for(self, topic: str) -> str:
        """把 EventBus 的 topic 名映射到 NATS subject 名。"""
        return f"{self.subject_prefix}{topic}"

    def durable_name_for(self, role: str, topic: str) -> str:
        """根据 process_role 和 topic 派生 JetStream durable consumer name。"""
        safe_topic = topic.replace(".", "_").replace(" ", "_")
        return f"{self.durable_name_prefix}{role}-{safe_topic}"


# ─────────────────────────────────────────────────────────────────────
# Consumer config spec（与 nats-py 解耦的描述）
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ConsumerConfigSpec:
    """JetStream consumer config 的纯 Python 描述（与 nats-py 解耦）。

    存在意义：把"NatsBusConfig → JetStream consumer 参数"的派生逻辑抽出来，
    让单元测试无需 import nats-py 也能断言 ack_wait/max_ack_pending/max_deliver
    确实从 NatsBusConfig 流到了订阅创建路径上。subscribe() 内部再把这个 spec
    翻译成 nats.js.api.ConsumerConfig。
    """

    durable_name: str
    ack_wait_seconds: float
    max_ack_pending: int
    max_deliver: int


def build_consumer_config_spec(
    *,
    config: NatsBusConfig,
    durable: str,
) -> ConsumerConfigSpec:
    """从 NatsBusConfig 派生 ConsumerConfigSpec。

    这是一个纯函数，纯粹为了让单元测试能断言"配置项确实被正确读取并传递"，
    不依赖任何 NATS server 或 nats-py 类型。
    """
    return ConsumerConfigSpec(
        durable_name=durable,
        ack_wait_seconds=config.ack_wait_seconds,
        max_ack_pending=config.max_ack_pending,
        max_deliver=config.max_deliver,
    )


# ─────────────────────────────────────────────────────────────────────
# NatsEventBus 骨架
# ─────────────────────────────────────────────────────────────────────


class NatsEventBus(EventBus):
    """NATS JetStream 实现的 EventBus（Stage 4 骨架）。

    使用方式（Stage 4 落地后）::

        bus = NatsEventBus(
            config=NatsBusConfig(),
            event_store=event_store,
            persistence_mode="strict",
            consumer_role="decision",
        )
        await bus.connect()
        await bus.ensure_stream(subjects=["aats.decisions", "aats.execution_intents"])
        await bus.subscribe("decisions", on_decision)
        ...
        await bus.close()

    ⚠️ 在 build_runtime 真正集成之前，本类的 connect()/subscribe() 不会被
    monolith 调用；__init__ 也不做任何 I/O，可以安全地被 import。
    """

    def __init__(
        self,
        config: NatsBusConfig,
        *,
        event_store: EventStore | None = None,
        persistence_mode: str = "strict",
        consumer_role: str = "monolith",
    ) -> None:
        self._config = config
        self._event_store = event_store
        self._persistence_mode = persistence_mode
        self._consumer_role = consumer_role
        self._client: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._subscriptions: list[Any] = []
        self._connected = False
        self.logger = get_logger("aats.event_bus.nats")

    # ── 生命周期 ────────────────────────────────────────────────
    async def start(self, *, topics: list[str] | None = None) -> None:
        """便利方法：connect + ensure_stream 一次完成。

        Stage 4 集成时 build_runtime 会调用本方法启动 NATS bus；
        在 _construct_event_bus 之后单独调用，避免让 _build_shared_runtime_slice
        变成 async 函数（其他 slice builder 都是 sync，保持对称）。
        """
        await self.connect()
        if topics:
            await self.ensure_stream(topics=topics)

    async def connect(self) -> None:
        """惰性连接 NATS server。"""
        if self._connected:
            return
        try:
            import nats  # type: ignore[import-not-found]  # noqa: F401  # 可选依赖
        except ImportError as exc:
            raise RuntimeError(
                "nats-py is required for NatsEventBus. "
                "Install with: pip install nats-py"
            ) from exc
        from nats.aio.client import Client as NATSClient  # type: ignore[import-not-found]

        client = NATSClient()
        await client.connect(
            servers=list(self._config.servers),
            name=self._config.name,
            connect_timeout=self._config.connect_timeout_seconds,
            max_reconnect_attempts=self._config.max_reconnect_attempts,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnected,
            reconnected_cb=self._on_reconnected,
            closed_cb=self._on_closed,
        )
        self._client = client
        self._js = client.jetstream()
        self._connected = True
        log_event(
            self.logger,
            "nats_event_bus_connected",
            servers=list(self._config.servers),
            consumer_role=self._consumer_role,
        )

    async def ensure_stream(self, topics: list[str]) -> None:
        """声明 JetStream stream（幂等）。

        Args:
            topics: EventBus topic 名列表（不带 ``aats.`` 前缀）。内部会用
                ``NatsBusConfig.subject_for`` 自动加前缀，调用方完全不需要
                关心 NATS subject 命名约定。

        Stage 4 集成时会在启动阶段被调用一次，确保 critical topic 都有持久化。
        ``stream_max_age_seconds`` 控制消息保留时长（默认 7 天），来自
        ``NatsBusConfig``，可在生产/dev/审计场景下分别配置。
        """
        if self._js is None:
            raise RuntimeError("NatsEventBus.ensure_stream called before connect()")
        try:
            from nats.js.api import (  # type: ignore[import-not-found]
                DiscardPolicy,
                RetentionPolicy,
                StorageType,
                StreamConfig,
            )
        except ImportError as exc:
            raise RuntimeError("nats-py JetStream API unavailable") from exc

        subjects = [self._config.subject_for(topic) for topic in topics]
        # nats-py StreamConfig.max_age 字段以**秒**为单位（见
        # nats/js/api.py: ``max_age: Optional[float] = None  # in seconds``），
        # 内部 _to_nanoseconds() 自行换算。这里**直接传秒**，不要预先乘 1e9，
        # 否则会被双重换算成超大整数，触发 NATS server "invalid JSON" 拒绝。
        config = StreamConfig(
            name=self._config.stream_name,
            subjects=subjects,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            discard=DiscardPolicy.OLD,
            max_age=self._config.stream_max_age_seconds,
        )
        await self._js.add_stream(config=config)
        log_event(
            self.logger,
            "nats_jetstream_stream_ensured",
            stream=self._config.stream_name,
            topics=topics,
            subjects=subjects,
            max_age_seconds=self._config.stream_max_age_seconds,
        )

    async def close(self) -> None:
        """优雅关闭：取消订阅 + 断开连接。"""
        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception as exc:
                log_event(
                    self.logger,
                    "nats_unsubscribe_failed",
                    level="warning",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        self._subscriptions.clear()
        if self._client is not None:
            await self._client.drain()
            self._client = None
        self._js = None
        self._connected = False

    # ── EventBus 接口 ────────────────────────────────────────────
    async def publish(self, topic: str, key: str, payload: dict) -> None:
        await self.publish_envelope(EventEnvelope.model_validate(payload), persist=True)

    async def publish_envelope(
        self,
        envelope: EventEnvelope,
        *,
        persist: bool = True,
    ) -> None:
        # 同步落盘到本地 event_store（双写：JetStream + Postgres）
        # 之所以保留 Postgres 落盘，是为了：
        #   1) 跨进程查询历史事件时复用现有 SQL 索引和 dashboard 视图
        #   2) JetStream 7 天 max_age 之外的长期可追溯性
        if persist and self._event_store is not None:
            try:
                await asyncio.to_thread(self._event_store.append, envelope)
            except Exception as exc:
                log_event(
                    self.logger,
                    "event_persistence_failed",
                    level="error",
                    topic=envelope.topic,
                    key=envelope.key,
                    persistence_mode=self._persistence_mode,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if self._persistence_mode == "strict":
                    raise

        if self._js is None:
            raise RuntimeError("NatsEventBus.publish called before connect()")

        subject = self._config.subject_for(envelope.topic)
        body = envelope.model_dump_json().encode("utf-8")
        # JetStream publish 返回 ack，包含 stream/sequence；同步等待是为了
        # 在 strict 模式下 publish 失败立即向 caller 抛错。
        await self._js.publish(subject=subject, payload=body)

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        """订阅 topic：注册一个 durable JetStream consumer。

        Consumer 的 ack_wait / max_ack_pending / max_deliver 全部从
        ``NatsBusConfig`` 派生（见 :func:`build_consumer_config_spec`），
        所以反压、超时重投、死信丢弃都是按 NatsBusConfig 配置生效，而不是
        nats-py 默认值。
        """
        if self._js is None:
            raise RuntimeError("NatsEventBus.subscribe called before connect()")

        try:
            from nats.js.api import (  # type: ignore[import-not-found]
                AckPolicy,
                ConsumerConfig,
            )
        except ImportError as exc:
            raise RuntimeError("nats-py JetStream API unavailable") from exc

        subject = self._config.subject_for(topic)
        durable = self._config.durable_name_for(self._consumer_role, topic)
        spec = build_consumer_config_spec(config=self._config, durable=durable)

        async def _on_msg(msg: Any) -> None:
            try:
                payload_dict = json.loads(msg.data.decode("utf-8"))
                envelope = EventEnvelope.model_validate(payload_dict)
                message = {
                    "topic": envelope.topic,
                    "key": envelope.key,
                    "payload": envelope.model_dump(mode="json"),
                }
                await handler(message)
                await msg.ack()
            except Exception as exc:
                log_event(
                    self.logger,
                    "nats_handler_error",
                    level="error",
                    topic=topic,
                    durable=durable,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                # 不 ack：JetStream 会按 ack_wait 重投
                # 超过 max_deliver 后会被丢到死信主题
                try:
                    await msg.nak()
                except Exception:
                    pass

        consumer_config = ConsumerConfig(
            durable_name=spec.durable_name,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=spec.ack_wait_seconds,
            max_ack_pending=spec.max_ack_pending,
            max_deliver=spec.max_deliver,
        )
        sub = await self._js.subscribe(
            subject=subject,
            durable=durable,
            cb=_on_msg,
            manual_ack=True,
            config=consumer_config,
        )
        self._subscriptions.append(sub)
        log_event(
            self.logger,
            "nats_subscription_registered",
            topic=topic,
            subject=subject,
            durable=durable,
            ack_wait_seconds=spec.ack_wait_seconds,
            max_ack_pending=spec.max_ack_pending,
            max_deliver=spec.max_deliver,
        )

    # ── NATS 回调 ────────────────────────────────────────────────
    async def _on_error(self, exc: Exception) -> None:
        log_event(
            self.logger,
            "nats_client_error",
            level="error",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    async def _on_disconnected(self) -> None:
        log_event(self.logger, "nats_client_disconnected", level="warning")

    async def _on_reconnected(self) -> None:
        log_event(self.logger, "nats_client_reconnected")

    async def _on_closed(self) -> None:
        log_event(self.logger, "nats_client_closed")


# ─────────────────────────────────────────────────────────────────────
# HybridEventBus：critical → NATS, observer → memory
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class HybridBusRouting:
    critical_topics: frozenset[str] = field(default_factory=lambda: DEFAULT_CRITICAL_TOPICS)
    observer_topics: frozenset[str] = field(default_factory=lambda: DEFAULT_OBSERVER_TOPICS)
    # 未在白名单内的 topic 路由策略：
    #   None       = 严格模式，未知 topic 抛 UnroutedTopicError（5c 后默认）
    #   "critical" = 默认走 NATS（保守，事件不丢；老语义，仅显式构造时可用）
    #   "observer" = 默认走 memory（性能优先；同上）
    # 默认 None 是 Stage 5c 引入的，目的：消除 Stage 4 路由表错位却被 fallback
    # 蒙混过关的隐患。强制开发者在加新 topic 时显式归类。
    default_route: str | None = None

    def route_for(self, topic: str) -> str:
        if topic in self.critical_topics:
            return "critical"
        if topic in self.observer_topics:
            return "observer"
        if self.default_route is None:
            raise UnroutedTopicError(
                f"topic {topic!r} 未在 critical / observer 任一集合中归类。"
                f" 请在 aats/bus/nats_bus.py 的 DEFAULT_CRITICAL_TOPICS 或"
                f" DEFAULT_OBSERVER_TOPICS 中显式加入；或在构造 HybridBusRouting"
                f" 时传入 default_route='critical'/'observer' 显式选择 fallback。"
            )
        return self.default_route


class HybridEventBus(EventBus):
    """混合 EventBus：按 topic 路由到 NATS 或内存。

    Stage 4 集成思路：
        1) build_runtime 构造 InMemoryEventBus 作为 observer_bus
        2) 同时构造 NatsEventBus 作为 critical_bus
        3) 用 HybridEventBus 包一层，对外暴露统一 EventBus 接口
        4) decision/execution/risk 模块发的事件自动走 NATS（跨进程）
        5) dashboard refresh / metrics 留在内存（高频低代价）
    """

    def __init__(
        self,
        *,
        critical_bus: EventBus,
        observer_bus: EventBus,
        routing: HybridBusRouting | None = None,
    ) -> None:
        self._critical = critical_bus
        self._observer = observer_bus
        self._routing = routing or HybridBusRouting()
        self.logger = get_logger("aats.event_bus.hybrid")

    @property
    def critical_bus(self) -> EventBus:
        """暴露 critical bus 供 build_runtime / shutdown 路径访问其生命周期方法。"""
        return self._critical

    @property
    def observer_bus(self) -> EventBus:
        return self._observer

    @property
    def routing(self) -> HybridBusRouting:
        return self._routing

    async def start(self) -> None:
        """启动两条底层总线：将 critical_topics 透传给关键总线。

        这一层是为了让 build_runtime 调用方只需要 ``await bus.start()``
        即可，不必关心底层是 InMemoryEventBus（无 start）还是 NatsEventBus
        （需要 connect + ensure_stream）。
        """
        critical_topics = sorted(self._routing.critical_topics)
        critical_start = getattr(self._critical, "start", None)
        if critical_start is not None:
            await critical_start(topics=critical_topics)
        observer_start = getattr(self._observer, "start", None)
        if observer_start is not None:
            await observer_start()

    async def close(self) -> None:
        """优雅关闭两条底层总线（best-effort，单条失败不影响另一条）。"""
        for bus_name, bus in (("critical", self._critical), ("observer", self._observer)):
            close_method = getattr(bus, "close", None)
            if close_method is None:
                continue
            try:
                await close_method()
            except Exception as exc:
                log_event(
                    self.logger,
                    "hybrid_bus_close_failed",
                    level="warning",
                    bus=bus_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def _select(self, topic: str) -> EventBus:
        return self._critical if self._routing.route_for(topic) == "critical" else self._observer

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        await self._select(topic).publish(topic, key, payload)

    async def publish_envelope(
        self,
        envelope: EventEnvelope,
        *,
        persist: bool = True,
    ) -> None:
        bus = self._select(envelope.topic)
        # 不是所有 EventBus 实现都暴露 publish_envelope；如果没有就退回到 publish
        publish_envelope: Callable[..., Awaitable[None]] | None = getattr(
            bus,
            "publish_envelope",
            None,
        )
        if publish_envelope is not None:
            await publish_envelope(envelope, persist=persist)
        else:
            await bus.publish(envelope.topic, envelope.key, envelope.model_dump(mode="json"))

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        await self._select(topic).subscribe(topic, handler)
