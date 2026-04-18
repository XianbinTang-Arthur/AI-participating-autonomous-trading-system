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
import os
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.telemetry import (
    extract_trace_context,
    inject_trace_context,
    start_span,
)
from aats.bus.base import EventBus, MessageHandler
from aats.events import topics as _topics
from aats.schemas.common import EventEnvelope
from aats.storage.base import EventStore
from aats.storage.stream_snapshot_cache import STREAM_CACHE_TOPICS as _STREAM_CACHE_TOPICS, StreamSnapshotCache

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from nats.aio.client import Client as NATSClient
    from nats.js import JetStreamContext


# R3-P1-X5：consumer 端 schema_version 兼容性校验用的主版本号。
# EventEnvelope.schema_version 默认是 "1.0.0"；当前所有事件都处于 1.x.y 的
# semver 主版本下，新增 optional 字段不算 breaking（MINOR 升），字段重命名 /
# 类型变更才需要 MAJOR 升级（从 "1.x.y" → "2.0.0"）。
# consumer 在收到 MAJOR 不同的 envelope 时应当直接 term() 掉，避免旧版本进程
# 去解一个结构已变化的消息时静默跑错逻辑。
_SUPPORTED_ENVELOPE_SCHEMA_MAJOR: str = "1"


def _envelope_schema_compatible(schema_version: str | None) -> bool:
    """返回 True 表示当前进程能安全解析该 envelope。

    按 semver 主版本匹配：只要字符串以 "{_SUPPORTED_ENVELOPE_SCHEMA_MAJOR}." 开头就视为兼容。
    空串 / None / 非 semver 形式一律视为不兼容（term 掉，避免歧义）。
    """
    if not isinstance(schema_version, str) or not schema_version:
        return False
    prefix = f"{_SUPPORTED_ENVELOPE_SCHEMA_MAJOR}."
    # "1" 单独也算兼容（pre-semver fallback）；"1.x.y" 走前缀匹配
    return schema_version == _SUPPORTED_ENVELOPE_SCHEMA_MAJOR or schema_version.startswith(prefix)


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
        _topics.OBLIGATION_UPDATES,       # Stage 6 Slice 6.5：obligation 广播；
                                          # decision risk / gateway dashboard 读路径依赖，
                                          # 决定跨进程 obligation 视图收敛不能丢
        _topics.FILL_EVENTS,              # 成交；资金变动核心
        _topics.PORTFOLIO_BALANCE_DELTAS, # 余额变动镜像
        _topics.ACCOUNT_SNAPSHOTS,        # 账户快照；非 execution 角色读取
        _topics.PORTFOLIO_SNAPSHOTS,      # 组合快照
        _topics.RECONCILIATION_REPORTS,   # 对账报告
        _topics.RECONCILIATION_VALIDATIONS,  # 对账验证
        _topics.REPLAY_VALIDATIONS,       # 回放验证；合规追溯
        # ── 审计 / operator / 错误流 ────────────────────────
        _topics.AUDIT_RECORDS,            # 审计记录；合规不能丢
        _topics.OPERATOR_ACTIONS,         # operator 人工动作驱动状态变化
        _topics.OPERATOR_COMMAND_REQUESTS,  # Slice 4-proc operator command proxy:
                                            # gateway→execution 请求 topic（rebaseline/resume
                                            # 依赖 execution-only service，走代理）
        _topics.OPERATOR_COMMAND_RESPONSES, # 同上，execution→gateway 响应 topic；
                                            # 两条都归 critical 防丢包卡 HTTP handler
        _topics.EXECUTION_ERROR_SUMMARIES,    # 执行错误汇总；驱动 risk 降级
        _topics.PROCESSING_FAILURES,      # 处理失败；同上
        _topics.KILL_SWITCH_STATE,        # Stage 6 Slice 6.2：kill_switch 跨进程同步
        _topics.GUARD_SIGNAL_UPDATES,     # guard signal 跨进程缓存；execution→decision，
                                          # 丢失 = decision 侧 120s 后 fail-closed 锁死交易。
                                          # 之前误放在 OBSERVER_TOPICS（内存 bus only），
                                          # 导致 decision 进程永远收不到更新。
        # ── strategy profile 切换路径 ─────────────────────
        _topics.STRATEGY_PROFILE_RECOMMENDATIONS,    # profile 推荐
        _topics.STRATEGY_PROFILE_ACTIVATIONS,        # profile 激活；影响实盘
        _topics.STRATEGY_PROFILE_REJECTIONS,         # profile 拒绝；状态记录
        _topics.STRATEGY_PROFILE_SELECTION_DECISIONS,    # profile 选择决策
        _topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES, # 自动回滚规则配置
        _topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,    # 激活规则配置
        # ── decision→gateway 跨进程报告 ────────────────────
        _topics.AI_PERFORMANCE_REPORTS,   # AI 表现报告；decision 生产，gateway API
                                          # GET /ai/performance/reports 消费。
                                          # 之前误放在 OBSERVER_TOPICS（内存 bus only），
                                          # 导致 gateway 永远收不到 AI 表现数据。
        _topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,  # 优化报告；decision 生产，
                                          # gateway API GET /strategy-profiles/
                                          # optimization/reports 消费。同上。
    }
)

# 观察者 topic：仪表盘/指标流/调试事件，量大、丢失无关键影响，留在内存
# ⚠️ 放入此集合的 topic 只走进程内 InMemoryEventBus，**绝不**通过 NATS
# 跨进程。如果一个 topic 的生产者和消费者在不同进程，它**必须**在
# DEFAULT_CRITICAL_TOPICS 里。
DEFAULT_OBSERVER_TOPICS: frozenset[str] = frozenset(
    {
        _topics.HEALTH_SNAPSHOTS,         # 系统健康指标；decision 内部，无消费者
        _topics.BLOCKER_SNAPSHOTS,        # operator dashboard 阻塞展示；gateway 内部闭环
        _topics.STRATEGY_PROFILE_EVALUATIONS,         # profile 评估输入；decision 内部
        _topics.STRATEGY_PROFILE_COMPARISON_REPORTS,  # profile 比较报告；decision 内部
    }
)

# ── Topic 投递语义分类（Slow Consumer 防护）─────────────────────────
#
# 分类决定 JetStream consumer 的 DeliverPolicy：
# - snapshot: 只需最新状态，历史在重启后无意义 → DeliverPolicy.LAST
# - transient: 请求-响应 / correlation-id 匹配，历史无用 → DeliverPolicy.NEW
# - event: 必须逐条处理，保证正确性 → DeliverPolicy.ALL（默认不变）
#
# 所有 consumer 同时启用 flow_control + idle_heartbeat，确保 NATS server
# 按 client ack 速率推送，从根本上避免 write_deadline 超时。
#
# 设计文档：plans/goofy-leaping-fox.md §第一步

SNAPSHOT_DELIVERY_TOPICS: frozenset[str] = frozenset(
    {
        _topics.MARKET_SNAPSHOTS,              # 高频行情快照，几秒后到达下一条
        _topics.FEATURE_SNAPSHOTS,             # 衍生特征快照，同上
        _topics.PORTFOLIO_SNAPSHOTS,           # 仓位快照，只需最新
        _topics.ACCOUNT_SNAPSHOTS,             # 账户快照，只需最新
        _topics.KILL_SWITCH_STATE,             # 熔断状态，Redis 兜底
        _topics.GUARD_SIGNAL_UPDATES,          # guard signal 快照，只需最新值
        _topics.STRATEGY_COORDINATOR_SNAPSHOTS,# 无状态全量快照，只需最新
        # ⚠️ ACCOUNT_BASELINES 不在此列：低频但每条有状态意义（operator 可连续
        # rebaseline），用 DeliverAll 保证不丢中间状态变更。
    }
)

TRANSIENT_DELIVERY_TOPICS: frozenset[str] = frozenset(
    {
        _topics.OPERATOR_COMMAND_REQUESTS,     # 操作员命令请求
        _topics.OPERATOR_COMMAND_RESPONSES,    # 操作员命令响应
    }
)


DeliverySemantics = Literal["snapshot", "transient", "event"]
DeliverPolicyStr = Literal["all", "last", "new"]


def delivery_semantics_for(topic: str) -> DeliverySemantics:
    """返回 topic 的投递语义: ``"snapshot"`` | ``"transient"`` | ``"event"``.

    - snapshot → ``DeliverPolicy.LAST``（只收最新一条）
    - transient → ``DeliverPolicy.NEW``（只收订阅后的新消息）
    - event → ``DeliverPolicy.ALL``（回放全部，但有 flow control 限速）
    """
    if topic in SNAPSHOT_DELIVERY_TOPICS:
        return "snapshot"
    if topic in TRANSIENT_DELIVERY_TOPICS:
        return "transient"
    return "event"


class UnroutedTopicError(KeyError):
    """未在 critical / observer 任一集合中归类的 topic 被请求路由时抛出。

    Why: HybridBusRouting 默认 default_route=None 时，未知 topic 必须抛错而不是
    silent fallback。这避免了 Stage 4 那种 "路由表错位 + fallback 蒙混过关" 的
    隐患——一旦有人加新 topic 但忘记归类，系统会立刻在 publish/subscribe 第一次
    调用时炸响而不是默默走错路径。
    """


# ─────────────────────────────────────────────────────────────────────
# StreamSpec（分层 stream 抽象，slice nats-capacity 引入）
#
# 设计依据：docs/task/slice_nats_jetstream_capacity_fix_design.md §4 + §7
#
# 核心动机：pre-slice 单 stream AATS_EVENTS 只设 max_age=7d，不设 max_bytes/
# max_msgs，结果 market.snapshots/feature.snapshots 高频写入把 stream 写到
# server max_file_store=1GB 硬上限，NATS 抛 err_code=10023 "insufficient
# resources" publish 永久失败，决策链路饿死无法下单。
#
# 修复方向：分层 2 stream
#   - AATS_EVENTS_MARKET: 1 day / 2 GB 承载 market.snapshots + feature.snapshots
#   - AATS_EVENTS      : 7 day / 4 GB 承载其他 critical events（合规保留窗口）
#   - server max_file_store 升 8 GB 留 25% headroom
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class StreamSpec:
    """NATS JetStream stream 的完整声明式定义。

    一个 StreamSpec 对应一个 ``js.add_stream`` / ``update_stream`` 调用的配置。
    本类是 nats-py ``StreamConfig`` 的薄封装 + 本项目的归属/容量约束，同时是
    ``NatsBusConfig.streams`` 字段的元素类型。

    设计依据：slice_nats_jetstream_capacity_fix_design.md §7.1
    """

    # ── 标识 ────────────────────────────────────────────────────
    name: str                     # JetStream stream 名（SCREAMING_SNAKE_CASE，如 "AATS_EVENTS"）
    topics: frozenset[str]        # 该 stream 承载的 EventBus topic 名（不带 aats. 前缀）

    # ── 容量策略（全部必填，没有默认值逼迫 caller 显式决策） ──
    max_age_seconds: float        # 消息保留时间上限（秒）
    max_bytes: int                # 总字节上限
    max_msgs: int                 # 总消息数上限
    max_msg_size: int             # 单条消息字节上限

    # ── 行为策略（有默认值，稳定字段） ─────────────────────────
    storage: str = "file"         # "file" / "memory"；传给 nats-py 时再转 StorageType
    retention: str = "limits"     # 固定 "limits"（本项目不用 workqueue/interest）
    discard: str = "old"          # 固定 "old"

    # ── 副本 / dedup / 运维（slice nats-capacity 新增，带安全默认） ──
    num_replicas: int = 1                     # dev 单节点；生产升级另起 slice
    duplicate_window_seconds: float = 120.0   # nats 默认；抗 publish 重试 + 进程重启 dedup
    deny_purge: bool = False                  # dev 允许 migration 脚本 purge；生产可 True

    def __post_init__(self) -> None:
        if not self.name or not self.name.isupper() or not self.name.replace("_", "").isalnum():
            raise ValueError(
                f"stream name must be SCREAMING_SNAKE_CASE, got {self.name!r}"
            )
        if not self.topics:
            raise ValueError(f"stream {self.name} must have at least one topic")
        if self.max_age_seconds <= 0:
            raise ValueError(
                f"stream {self.name} max_age_seconds must be positive, "
                f"got {self.max_age_seconds}"
            )
        if self.max_bytes <= 0:
            raise ValueError(
                f"stream {self.name} max_bytes must be positive, got {self.max_bytes}"
            )
        if self.max_msgs <= 0:
            raise ValueError(
                f"stream {self.name} max_msgs must be positive, got {self.max_msgs}"
            )
        if self.max_msg_size <= 0:
            raise ValueError(
                f"stream {self.name} max_msg_size must be positive, got {self.max_msg_size}"
            )
        if self.storage not in ("file", "memory"):
            raise ValueError(
                f"stream {self.name} storage must be 'file' or 'memory', "
                f"got {self.storage!r}"
            )
        if self.num_replicas < 1:
            raise ValueError(
                f"stream {self.name} num_replicas must be >= 1, got {self.num_replicas}"
            )
        if self.duplicate_window_seconds < 0:
            raise ValueError(
                f"stream {self.name} duplicate_window_seconds must be >= 0, "
                f"got {self.duplicate_window_seconds}"
            )

    def to_nats_stream_config(self, subject_prefix: str) -> Any:
        """转成 nats-py StreamConfig 对象，调用 ensure_streams 时用。

        subject_prefix 由 caller 提供（通常是 ``NatsBusConfig.subject_prefix``
        也就是 "aats."），拼在 topic 前得到完整的 NATS subject 名。

        Returns: ``nats.js.api.StreamConfig`` 实例（nats-py import 延迟到
            本方法内部避免 monolith 不装 nats-py 的模块级导入失败）。
        """
        from nats.js.api import (  # type: ignore[import-not-found]
            DiscardPolicy,
            RetentionPolicy,
            StorageType,
            StreamConfig,
        )

        subjects = [f"{subject_prefix}{topic}" for topic in sorted(self.topics)]
        return StreamConfig(
            name=self.name,
            subjects=subjects,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE if self.storage == "file" else StorageType.MEMORY,
            discard=DiscardPolicy.OLD,
            max_age=self.max_age_seconds,
            max_bytes=self.max_bytes,
            max_msgs=self.max_msgs,
            max_msg_size=self.max_msg_size,
            num_replicas=self.num_replicas,
            duplicate_window=self.duplicate_window_seconds,
            deny_purge=self.deny_purge,
        )


def _compute_stream_config_drift(
    existing: Any,
    spec: StreamSpec,
    desired_subjects: list[str],
) -> dict[str, dict[str, Any]]:
    """比较一个已经存在的 ``nats.js.api.StreamConfig`` 与 StreamSpec 的差异。

    Args:
        existing: ``nats.js.api.StreamConfig``（从 ``stream_info().config`` 拿）
        spec: 目标 StreamSpec
        desired_subjects: caller 已经算好的完整 NATS subject 列表（带前缀），
            由 ``spec.to_nats_stream_config(subject_prefix).subjects`` 派生。

    Returns:
        空 dict 表示完全匹配（走 unchanged 分支）；
        非空 dict 的 key 是 drift 字段名，value 是
        ``{"existing": ..., "desired": ...}`` 的 diff 快照，用于日志 + update 判断。

    比较维度（设计文档 §7.4 - 超过 Slice 6.5 的 subjects-only 对比）：
        subjects / max_age / max_bytes / max_msgs / max_msg_size /
        num_replicas / duplicate_window / deny_purge

    注意：
    - ``subjects`` 比较用 set（顺序无关）
    - ``max_age`` / ``duplicate_window`` 的 nats-py 类型可能是 float 秒，也
      可能是 int 纳秒 —— 我们用 spec 的秒值和 existing 的 float 对比，
      如果 existing 明显大于 10^10 (10 秒 × 10^9 纳秒 = 阈值)，则当成纳秒除回秒。
    """
    drift: dict[str, dict[str, Any]] = {}

    # ── subjects（set 比较，顺序无关）──
    existing_set = set(getattr(existing, "subjects", None) or [])
    desired_set = set(desired_subjects)
    if existing_set != desired_set:
        drift["subjects"] = {
            "existing": sorted(existing_set),
            "desired": sorted(desired_set),
        }

    # ── max_age：秒/纳秒兼容 ──
    existing_max_age = getattr(existing, "max_age", None)
    if existing_max_age is not None:
        # 阈值：10^10 = 10 秒 * 10^9 纳秒；超过就当纳秒除回秒
        if isinstance(existing_max_age, (int, float)) and existing_max_age > 1e10:
            existing_max_age = existing_max_age / 1e9
    if existing_max_age != spec.max_age_seconds:
        drift["max_age_seconds"] = {
            "existing": existing_max_age,
            "desired": spec.max_age_seconds,
        }

    # ── 容量字段（直接比较）──
    for attr, spec_field in (
        ("max_bytes", "max_bytes"),
        ("max_msgs", "max_msgs"),
        ("max_msg_size", "max_msg_size"),
        ("num_replicas", "num_replicas"),
    ):
        existing_val = getattr(existing, attr, None)
        desired_val = getattr(spec, spec_field)
        if existing_val != desired_val:
            drift[attr] = {"existing": existing_val, "desired": desired_val}

    # ── duplicate_window：同 max_age 秒/纳秒兼容 ──
    existing_dup = getattr(existing, "duplicate_window", None)
    if existing_dup is not None:
        if isinstance(existing_dup, (int, float)) and existing_dup > 1e10:
            existing_dup = existing_dup / 1e9
    if existing_dup != spec.duplicate_window_seconds:
        drift["duplicate_window_seconds"] = {
            "existing": existing_dup,
            "desired": spec.duplicate_window_seconds,
        }

    # ── deny_purge ──
    existing_deny = getattr(existing, "deny_purge", None)
    # nats-py 旧版可能没这个字段，None == False 视为默认值
    if (existing_deny or False) != spec.deny_purge:
        drift["deny_purge"] = {
            "existing": existing_deny,
            "desired": spec.deny_purge,
        }

    return drift


# 高频观察/派生 topic → 独立短保留 stream AATS_EVENTS_MARKET
#
# 注意：加新 topic 到 DEFAULT_CRITICAL_TOPICS 时必须同步判断归属：
#   - 高频（≥ 1 Hz 写入）且纯观察/派生 → 加到这里
#   - 低频决策/执行/审计/资金状态 → 留在 DEFAULT_CRITICAL_EVENTS_TOPICS
# 单元测试 test_stream_specs_cover_all_critical_topics_exactly_once 会强制
# 每个新 topic 都必须归属到某一个 StreamSpec，漏了会红灯。
DEFAULT_MARKET_STREAM_TOPICS: frozenset[str] = frozenset(
    {
        _topics.MARKET_SNAPSHOTS,   # 最高频（OKX tick 推送）
        _topics.FEATURE_SNAPSHOTS,  # 次高频（market 派生）
    }
)

# 其他 critical topic → 长保留 stream AATS_EVENTS
# 派生自 DEFAULT_CRITICAL_TOPICS 减掉 DEFAULT_MARKET_STREAM_TOPICS，保证
# 两者并集 == DEFAULT_CRITICAL_TOPICS（I-8 不变量）。
DEFAULT_CRITICAL_EVENTS_TOPICS: frozenset[str] = (
    DEFAULT_CRITICAL_TOPICS - DEFAULT_MARKET_STREAM_TOPICS
)


# 默认的两个 stream spec（两者对称设计，见设计文档 §4.4）
#
# 容量预算对账（设计文档 §4.1）：
#   AATS_EVENTS_MARKET.max_bytes = 2 GB
#   AATS_EVENTS.max_bytes        = 4 GB
#   合计                         = 6 GB
#   server max_file_store        = 8 GB (nats-server.conf)
#   headroom                     = 2 GB (25%，留给 index + consumer state)
# 单元测试 test_total_stream_capacity_within_server_budget 锁死这个对齐。
DEFAULT_AATS_EVENTS_MARKET_SPEC = StreamSpec(
    name="AATS_EVENTS_MARKET",
    topics=DEFAULT_MARKET_STREAM_TOPICS,
    max_age_seconds=86_400,              # 1 天
    max_bytes=2_147_483_648,             # 2 GB
    max_msgs=5_000_000,                  # 500 万条保险丝
    max_msg_size=4_194_304,              # 4 MB（与 EVENTS 对称，对齐 server max_payload）
    # num_replicas / duplicate_window_seconds / deny_purge 走默认值
)

DEFAULT_AATS_EVENTS_SPEC = StreamSpec(
    name="AATS_EVENTS",
    topics=DEFAULT_CRITICAL_EVENTS_TOPICS,
    max_age_seconds=604_800,             # 7 天（合规 / 回放窗口）
    max_bytes=4_294_967_296,             # 4 GB
    max_msgs=5_000_000,                  # 500 万条
    max_msg_size=4_194_304,              # 4 MB（与 MARKET 对称）
)

DEFAULT_STREAM_SPECS: tuple[StreamSpec, ...] = (
    DEFAULT_AATS_EVENTS_MARKET_SPEC,
    DEFAULT_AATS_EVENTS_SPEC,
)


def build_nats_streams_from_env(
    default_specs: tuple[StreamSpec, ...] = DEFAULT_STREAM_SPECS,
) -> tuple[StreamSpec, ...]:
    """把 env var override 应用到默认 StreamSpec list。

    支持的 env var（slice nats-capacity §7.6）：
    - AATS_NATS_MARKET_MAX_BYTES / MAX_MSGS / MAX_MSG_SIZE / MAX_AGE_SECONDS
    - AATS_NATS_EVENTS_MAX_BYTES / MAX_MSGS / MAX_MSG_SIZE / MAX_AGE_SECONDS

    Args:
        default_specs: 默认 spec tuple，通常传 DEFAULT_STREAM_SPECS。

    Returns:
        新 tuple；如果没有 env override 则直接返回 default_specs 原样。
    """
    result: list[StreamSpec] = []
    for spec in default_specs:
        if spec.name == "AATS_EVENTS_MARKET":
            prefix = "AATS_NATS_MARKET_"
        elif spec.name == "AATS_EVENTS":
            prefix = "AATS_NATS_EVENTS_"
        else:
            prefix = None

        overrides: dict[str, int | float] = {}
        if prefix:
            if v := os.environ.get(f"{prefix}MAX_BYTES"):
                overrides["max_bytes"] = int(v)
            if v := os.environ.get(f"{prefix}MAX_MSGS"):
                overrides["max_msgs"] = int(v)
            if v := os.environ.get(f"{prefix}MAX_MSG_SIZE"):
                overrides["max_msg_size"] = int(v)
            if v := os.environ.get(f"{prefix}MAX_AGE_SECONDS"):
                overrides["max_age_seconds"] = float(v)
        result.append(replace(spec, **overrides) if overrides else spec)
    return tuple(result)


# ─────────────────────────────────────────────────────────────────────
# NATS 配置
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class NatsBusConfig:
    """NatsEventBus 实例化所需的配置。

    slice nats-capacity 双路径说明：
    - **runtime 路径**（生产 + 集成测试）：读 ``self.streams``，由
      ``_construct_event_bus`` 传入 ``build_nats_streams_from_env(DEFAULT_STREAM_SPECS)``，
      通过 ``NatsEventBus.ensure_streams()`` 无参方法完成多 stream upsert。
    - **legacy 路径**（单元测试 ``ensure_stream(topics=...)`` shim）：读 legacy
      字段 ``stream_name`` + ``stream_max_age_seconds``，构造一个临时 StreamSpec
      做 backward compat，发 DeprecationWarning。runtime 不再读 legacy 字段。

    设计依据：slice_nats_jetstream_capacity_fix_design.md §7.3
    """

    servers: tuple[str, ...] = ("nats://127.0.0.1:4222",)
    name: str = "aats"
    subject_prefix: str = "aats."
    # ── slice nats-capacity 新字段：runtime 分层 stream 拓扑 ──────────
    # runtime 代码路径（_construct_event_bus + HybridEventBus.start）只读
    # 这个字段，通过 ensure_streams() 无 topics 参数完成 upsert。
    # 默认是两条 stream（MARKET + EVENTS），caller 可以传
    # ``build_nats_streams_from_env(DEFAULT_STREAM_SPECS)`` 开启 env 覆盖。
    streams: tuple[StreamSpec, ...] = DEFAULT_STREAM_SPECS
    # ── Legacy 字段（只给 ensure_stream(topics=...) shim 用） ─────────
    # ⚠️ runtime 不再读这两个字段，只有 ``NatsEventBus.ensure_stream(topics)``
    # legacy shim 会临时构造一条 StreamSpec 时用作 name / max_age 默认。
    # 新代码应直接用 ``streams`` 字段。
    stream_name: str = "AATS_EVENTS"
    # JetStream stream 内消息最大保留时间（秒），过期消息会被自动丢弃。
    # 默认 7 天足够 Stage 4 集成回放，生产可调长（30/90 天）。
    stream_max_age_seconds: float = 7 * 24 * 60 * 60
    # 单条事件最大 ack_wait（秒），handler 处理超时后会被重试
    ack_wait_seconds: float = 30.0
    # 单个消费者最多 in-flight ack 待确认数
    max_ack_pending: int = 256
    # Fix P1-4：per-topic max_ack_pending 覆盖。key 为 topic 名（如
    # "aats.fill_events"），value 为该 consumer 的 max_ack_pending。
    # 未列出的 topic 使用上面的全局默认值。
    per_topic_max_ack_pending: dict[str, int] | None = None
    # 单条消息最大重投递次数（超出后会被丢入死信主题）
    max_deliver: int = 5
    # ── Slow consumer 防护（flow control + heartbeat）──────────────
    # 启用 per-consumer flow control：NATS server 在每批消息发送后
    # 等待 client 的 flow control ack 才继续推送，避免写缓冲区溢出。
    flow_control: bool = True
    # 空闲心跳间隔（秒）：防止 flow control 开启后连接因无数据超时断开。
    idle_heartbeat_seconds: float = 5.0
    # 连接超时
    connect_timeout_seconds: float = 5.0
    # 重连最大次数（-1 = 无限重连）
    max_reconnect_attempts: int = -1
    # 消费者持久 name 前缀（每个进程角色独立）
    durable_name_prefix: str = "aats-"

    def __post_init__(self) -> None:
        """校验 streams 字段的不变量（I-8 拓扑互斥 + 非空）。

        - streams 非空：至少有一条 stream
        - 名称唯一：不允许两条 stream 用同一个 name
        - topic 互斥：不允许同一个 topic 被多条 stream 同时 claim（否则
          subject overlap，nats-py add_stream 会抛 "subjects overlap" 错误）
        """
        if not self.streams:
            raise ValueError("NatsBusConfig.streams must be non-empty")

        seen_names: set[str] = set()
        seen_topics: dict[str, str] = {}  # topic -> stream_name
        for spec in self.streams:
            if spec.name in seen_names:
                raise ValueError(
                    f"NatsBusConfig.streams has duplicate stream name {spec.name!r}"
                )
            seen_names.add(spec.name)
            for topic in spec.topics:
                if topic in seen_topics:
                    raise ValueError(
                        f"NatsBusConfig.streams has topic {topic!r} claimed by both "
                        f"{seen_topics[topic]!r} and {spec.name!r}; each topic must "
                        f"belong to exactly one stream"
                    )
                seen_topics[topic] = spec.name

    def subject_for(self, topic: str) -> str:
        """把 EventBus 的 topic 名映射到 NATS subject 名。"""
        return f"{self.subject_prefix}{topic}"

    def durable_name_for(self, role: str, topic: str) -> str:
        """根据 process_role 和 topic 派生 JetStream durable consumer name。"""
        safe_topic = topic.replace(".", "_").replace(" ", "_")
        return f"{self.durable_name_prefix}{role}-{safe_topic}"

    def stream_spec_for_topic(self, topic: str) -> StreamSpec | None:
        """查询某个 topic 归属哪条 stream（用于 publish 路由 / 调试）。

        Returns: 匹配的 StreamSpec，如果 topic 不在任何 stream 的 topics
            集合中则返回 None（调用方判断是否归属 observer topic）。
        """
        for spec in self.streams:
            if topic in spec.topics:
                return spec
        return None


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
    # ── Slow consumer 防护字段 ──
    deliver_policy: DeliverPolicyStr = "all"
    flow_control: bool = False
    idle_heartbeat_seconds: float = 0.0   # 0 = 不启用


def build_consumer_config_spec(
    *,
    config: NatsBusConfig,
    durable: str,
    topic: str = "",
) -> ConsumerConfigSpec:
    """从 NatsBusConfig 派生 ConsumerConfigSpec。

    这是一个纯函数，纯粹为了让单元测试能断言"配置项确实被正确读取并传递"，
    不依赖任何 NATS server 或 nats-py 类型。

    ``topic`` 参数用于查询投递语义（snapshot/transient/event），
    决定 ``deliver_policy``。空字符串时默认 "event" → ``DeliverAll``。
    """
    semantics = delivery_semantics_for(topic) if topic else "event"
    if semantics == "snapshot":
        dp = "last"
    elif semantics == "transient":
        dp = "new"
    else:
        dp = "all"

    # Fix P1-4：per-topic max_ack_pending 覆盖
    effective_max_ack_pending = config.max_ack_pending
    if config.per_topic_max_ack_pending and topic in config.per_topic_max_ack_pending:
        effective_max_ack_pending = config.per_topic_max_ack_pending[topic]

    return ConsumerConfigSpec(
        durable_name=durable,
        ack_wait_seconds=config.ack_wait_seconds,
        max_ack_pending=effective_max_ack_pending,
        max_deliver=config.max_deliver,
        deliver_policy=dp,
        flow_control=config.flow_control,
        idle_heartbeat_seconds=config.idle_heartbeat_seconds,
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
        stream_snapshot_cache: StreamSnapshotCache | None = None,
    ) -> None:
        self._config = config
        self._event_store = event_store
        self._persistence_mode = persistence_mode
        self._consumer_role = consumer_role
        self._stream_cache = stream_snapshot_cache
        self._client: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._subscriptions: list[Any] = []
        self._connected = False
        self.logger = get_logger("aats.event_bus.nats")

    # ── 生命周期 ────────────────────────────────────────────────
    async def start(self, *, topics: list[str] | None = None) -> None:
        """便利方法：connect + ensure stream(s) 一次完成。

        双路径（slice nats-capacity §7.5）：

        - **runtime 路径**（topics 为 None，生产 + 集成测试）：调用
          ``ensure_streams()`` 无参方法，遍历 ``self._config.streams`` 做多
          stream upsert。这是 ``_construct_event_bus`` 传下来的正式路径。

        - **legacy 路径**（topics 非 None，单元测试）：发 DeprecationWarning
          + 走 ``ensure_stream(topics=topics)`` 老 shim，为了向后兼容
          ``tests/unit/test_nats_bus_skeleton.py`` 里调 ``start(topics=[...])``
          的测试。新代码不要传 topics 参数。

        Stage 4 集成时 build_runtime 会调用本方法启动 NATS bus；
        在 _construct_event_bus 之后单独调用，避免让 _build_shared_runtime_slice
        变成 async 函数（其他 slice builder 都是 sync，保持对称）。
        """
        await self.connect()
        if topics is None:
            await self.ensure_streams()
        else:
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

    # ── Stream ensure（slice nats-capacity 双路径） ──────────────
    #
    # 两条独立路径：
    #   runtime 新路径  : ensure_streams() 无参 → 遍历 self._config.streams
    #                    → _ensure_single_stream(spec) 做容量感知三分支 upsert
    #   legacy 老路径  : ensure_stream(topics=...) shim → 发 DeprecationWarning
    #                    → 从 legacy 字段 + 测试默认容量构造一次性 StreamSpec
    #                    → 同样走 _ensure_single_stream(spec)
    # 两条路径最终都汇聚到 _ensure_single_stream，保证三分支逻辑只有一份实现。
    #
    # 设计文档：slice_nats_jetstream_capacity_fix_design.md §7.4

    async def ensure_streams(self) -> None:
        """runtime 路径：遍历 ``self._config.streams`` 做多 stream upsert。

        这是新代码应该走的路径。``_construct_event_bus`` 构造 ``NatsBusConfig``
        时把 ``streams=build_nats_streams_from_env(DEFAULT_STREAM_SPECS)`` 传入，
        所以到这里 ``self._config.streams`` 已经是完整的分层 stream 拓扑。

        每条 stream 的 upsert 委托给 ``_ensure_single_stream(spec)`` 处理
        容量感知三分支逻辑（NotFoundError / unchanged / updated）。

        全部成功后 emit 一个汇总事件 ``nats_jetstream_streams_ensured``
        供 runbook / dashboard 做冷烟断言。
        """
        if self._js is None:
            raise RuntimeError(
                "NatsEventBus.ensure_streams called before connect()"
            )

        ensured_names: list[str] = []
        for spec in self._config.streams:
            await self._ensure_single_stream(spec)
            ensured_names.append(spec.name)

        log_event(
            self.logger,
            "nats_jetstream_streams_ensured",
            stream_count=len(ensured_names),
            stream_names=ensured_names,
        )

    async def ensure_stream(self, topics: list[str]) -> None:
        """Legacy shim：从 legacy 字段构造一次性 StreamSpec 做 upsert。

        .. deprecated::
            用 ``ensure_streams()`` 无参方法代替。本方法只为了向后兼容
            ``tests/unit/test_nats_bus_skeleton.py`` 里调
            ``start(topics=[...])`` 的单元测试，runtime 代码路径
            （_construct_event_bus + HybridEventBus.start）不再走本 shim。

            在调用本方法时会发 ``DeprecationWarning``，shim 保留期至少 3 个月
            （设计文档 §11.7），之后统一删除并把测试迁到 ``ensure_streams()``。

        Args:
            topics: EventBus topic 名列表（不带 ``aats.`` 前缀）。内部会构造
                一条临时 StreamSpec（name 取 ``config.stream_name``，max_age
                取 ``config.stream_max_age_seconds``，容量使用测试友好默认），
                然后走 ``_ensure_single_stream()`` 三分支逻辑。

        历史：Slice 6.5 把本方法改成幂等三分支（NotFoundError / unchanged /
        updated），slice nats-capacity 把它降级为 legacy shim 同时保留三分支
        语义 —— 区别只是现在容量字段也会参与比较（不只是 subjects）。
        """
        warnings.warn(
            "NatsEventBus.ensure_stream(topics=...) is deprecated; "
            "use ensure_streams() with NatsBusConfig.streams instead. "
            "See slice_nats_jetstream_capacity_fix_design.md §7.4.",
            DeprecationWarning,
            stacklevel=2,
        )
        if self._js is None:
            raise RuntimeError(
                "NatsEventBus.ensure_stream called before connect()"
            )

        # 从 legacy 字段 + 测试友好默认容量构造一次性 StreamSpec
        # 容量默认值的选取原则（设计文档 §7.4）：
        #   - max_bytes=128 MB   够单元测试假 publish 用，远低于 server 硬限
        #   - max_msgs=10_000    够单元测试假 publish 用
        #   - max_msg_size=4 MB  与 runtime 两条 stream 对称
        legacy_spec = StreamSpec(
            name=self._config.stream_name,
            topics=frozenset(topics),
            max_age_seconds=self._config.stream_max_age_seconds,
            max_bytes=128 * 1024 * 1024,   # 128 MB
            max_msgs=10_000,
            max_msg_size=4 * 1024 * 1024,  # 4 MB
        )
        await self._ensure_single_stream(legacy_spec)

    async def _ensure_single_stream(self, spec: StreamSpec) -> None:
        """对单条 StreamSpec 做容量感知幂等 upsert（三分支）。

        分支矩阵：

        1. ``stream_info`` 抛 ``NotFoundError``：首次创建 → ``add_stream``
           log: ``nats_jetstream_stream_created``
        2. ``stream_info`` 成功 + 当前 config 与 spec **完全匹配** → noop
           log: ``nats_jetstream_stream_unchanged``
        3. ``stream_info`` 成功 + 当前 config 与 spec **有差异** →
           ``update_stream`` 并在日志里打出 drift 字段列表
           log: ``nats_jetstream_stream_updated``

        匹配维度（超过 Slice 6.5 的 subjects-only 比较）：
            subjects / max_age / max_bytes / max_msgs / max_msg_size /
            num_replicas / duplicate_window / deny_purge

        任一路径成功后都会 emit 统一的 ``nats_jetstream_stream_ensured``
        收尾事件（向后兼容 Slice 6.1 / 6.3 的 runbook 冷烟断言）。
        """
        if self._js is None:
            raise RuntimeError(
                "NatsEventBus._ensure_single_stream called before connect()"
            )
        try:
            from nats.js.errors import (  # type: ignore[import-not-found]
                NotFoundError,
            )
        except ImportError as exc:
            raise RuntimeError("nats-py JetStream API unavailable") from exc

        config = spec.to_nats_stream_config(self._config.subject_prefix)
        subjects = list(config.subjects or [])

        # ── Step 1: stream_info 探测现状 ──────────────────────
        existing_info: Any | None = None
        try:
            existing_info = await self._js.stream_info(spec.name)
        except NotFoundError:
            existing_info = None

        if existing_info is None:
            # ── Step 2a: 不存在 → 创建 ────────────────────────
            await self._js.add_stream(config=config)
            log_event(
                self.logger,
                "nats_jetstream_stream_created",
                stream=spec.name,
                subject_count=len(subjects),
                max_age_seconds=spec.max_age_seconds,
                max_bytes=spec.max_bytes,
                max_msgs=spec.max_msgs,
                max_msg_size=spec.max_msg_size,
            )
        else:
            # ── Step 2b: 已存在 → 容量感知比较 ───────────────
            drift = _compute_stream_config_drift(
                existing_info.config,
                spec,
                desired_subjects=subjects,
            )
            if not drift:
                log_event(
                    self.logger,
                    "nats_jetstream_stream_unchanged",
                    stream=spec.name,
                    subject_count=len(subjects),
                )
            else:
                await self._js.update_stream(config=config)
                log_event(
                    self.logger,
                    "nats_jetstream_stream_updated",
                    stream=spec.name,
                    subject_count_after=len(subjects),
                    drift_fields=sorted(drift.keys()),
                    drift=drift,
                )

        # ── Step 3: 统一 "ensured" 收尾日志（向后兼容 Slice 6.1/6.3） ──
        log_event(
            self.logger,
            "nats_jetstream_stream_ensured",
            stream=spec.name,
            topics=sorted(spec.topics),
            subjects=subjects,
            max_age_seconds=spec.max_age_seconds,
            max_bytes=spec.max_bytes,
            max_msgs=spec.max_msgs,
            max_msg_size=spec.max_msg_size,
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
        if self._js is None:
            raise RuntimeError("NatsEventBus.publish called before connect()")

        subject = self._config.subject_for(envelope.topic)
        # Stage 8：publish 动作本身开一个 span；inject_trace_context 在本 span
        # 内调用，这样 OTel 捕获到的 carrier 会把 nats.publish.<topic> 作为
        # parent，而不是再往上一层的 handler span。结果是下游 consumer 的
        # nats.receive.<topic> 直接挂在 producer 的 nats.publish.<topic> 下面，
        # Jaeger 里可以清晰看到 publish→receive 的一对一因果链。
        # 设计文档：docs/task/stage_8_otel_integration_design.md §D3/§D4
        with start_span(
            f"nats.publish.{envelope.topic}",
            attributes={
                "aats.topic": envelope.topic,
                "aats.event_type": envelope.event_type,
                "aats.event_id": envelope.event_id,
                "aats.source_component": envelope.source_component,
                "messaging.system": "nats",
                "messaging.destination": subject,
            },
        ):
            # 在 nats.publish.<topic> span 内部 inject —— 下游 consumer 的
            # parent 指向本 span。inject 在没装 OTel 或没 active span 时是
            # no-op，空 carrier 保留，envelope.trace_context 继续是 None
            # （向后兼容）。
            carrier: dict[str, str] = {}
            try:
                inject_trace_context(carrier)
            except Exception as exc:  # pragma: no cover - 防御性兜底
                log_event(
                    self.logger,
                    "trace_context_inject_failed",
                    level="warning",
                    topic=envelope.topic,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                carrier = {}
            if carrier:
                envelope = envelope.model_copy(update={"trace_context": dict(carrier)})

            # ── 持久化 / 缓存分流 ────────────────────────────
            # 高频流式 topic（market.snapshots / features.snapshots）写入
            # 进程内 StreamSnapshotCache，不落 Postgres——避免 event_store
            # 表每 30 分钟膨胀到 400K 行导致 dashboard 查询超时。
            # 其余 topic 仍然双写 JetStream + Postgres。
            if self._stream_cache is not None and envelope.topic in _STREAM_CACHE_TOPICS:
                self._stream_cache.update(envelope)
            elif persist and self._event_store is not None:
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

            body = envelope.model_dump_json().encode("utf-8")
            # R3-P1-X4：激活 JetStream 发布端幂等（publish-side dedup）。
            # stream 层配置了 duplicate_window=120s（见 StreamConfigSpec），但
            # 只有在 publish 时带 Nats-Msg-Id header，JetStream 才会按这个 ID 在
            # 窗口内去重。没有 header 的话 duplicate_window 配置完全是摆设。
            #
            # 场景：outbox.flush_pending 对单条 envelope 最多 retry 3 次
            #   （_MAX_PUBLISH_ATTEMPTS=3，每次 5s 超时），加上 flush 之间的
            #   间隔，一条 envelope 的 retry 总窗口 <30s，远小于 120s，
            #   所以重试完全落在 duplicate_window 内：如果第一次 publish 实际
            #   到达了 broker 但 ack 路径超时，caller 视为失败并重试，JetStream
            #   会按 Nats-Msg-Id=event_id 识别为 duplicate 直接 ack 不重复入流。
            #
            # 进程崩溃后重启：未 PUBLISHED 的 outbox 行会被再次读出 publish，
            # 只要 event_id 稳定（=envelope.event_id），JetStream 同样去重。
            #
            # JetStream publish 返回 ack，包含 stream/sequence；同步等待是为了
            # 在 strict 模式下 publish 失败立即向 caller 抛错。
            await self._js.publish(
                subject=subject,
                payload=body,
                headers={"Nats-Msg-Id": envelope.event_id},
            )

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
                DeliverPolicy,
            )
            from nats.js.errors import BadRequestError  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("nats-py JetStream API unavailable") from exc

        subject = self._config.subject_for(topic)
        durable = self._config.durable_name_for(self._consumer_role, topic)
        spec = build_consumer_config_spec(
            config=self._config, durable=durable, topic=topic,
        )

        async def _on_msg(msg: Any) -> None:
            # ── Phase 1: 反序列化（无 trace context，失败走早期 return）──
            try:
                payload_dict = json.loads(msg.data.decode("utf-8"))
                envelope = EventEnvelope.model_validate(payload_dict)
            except Exception as exc:
                log_event(
                    self.logger,
                    "nats_message_parse_error",
                    level="error",
                    topic=topic,
                    durable=durable,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    permanent=True,
                )
                # 解析错误是确定性失败（坏 JSON / schema 不匹配），重投
                # 也不会成功。优先 term()（JetStream 2.10+ 支持）直接终止
                # 消息；如果 nats-py 版本不支持 term() 则回退到 nak()。
                try:
                    if hasattr(msg, "term"):
                        await msg.term()
                    else:
                        await msg.nak()
                except Exception:
                    pass
                return

            # R3-P1-X5：envelope 反序列化成功不等于版本兼容。如果生产端发了
            # schema_version="2.0.0" 的消息（未来某次 schema 变更），旧版本
            # consumer 由于字段是 Optional 默认值，pydantic 并不会报错就放过
            # 去，但业务语义已经被错误解读。在 consumer 入口按主版本号严格
            # 过滤，不兼容直接 term() 不再重投；避免旧进程静默跑错逻辑。
            if not _envelope_schema_compatible(envelope.schema_version):
                log_event(
                    self.logger,
                    "nats_envelope_schema_incompatible",
                    level="critical",
                    topic=topic,
                    durable=durable,
                    event_id=envelope.event_id,
                    event_type=envelope.event_type,
                    source_component=envelope.source_component,
                    envelope_schema_version=envelope.schema_version,
                    supported_major=_SUPPORTED_ENVELOPE_SCHEMA_MAJOR,
                    permanent=True,
                )
                try:
                    if hasattr(msg, "term"):
                        await msg.term()
                    else:
                        await msg.nak()
                except Exception:
                    pass
                return

            # 高频 topic 写入进程内缓存，供 latest() / recent() 查询
            if self._stream_cache is not None and envelope.topic in _STREAM_CACHE_TOPICS:
                self._stream_cache.update(envelope)
            # 非高频 topic: 写入 event_store，使**接收方**进程也持有跨进程
            # 事件副本。publish 路径已有相同分流逻辑（见 publish() 方法），
            # 此处补齐 receive 路径——否则非生产者进程（如 gateway）的
            # event_store 对跨进程 topic 永远为空，导致 dashboard 查询缺数据。
            # InMemoryEventStore.append 按 event_id 去重，不会重复写入。
            elif self._event_store is not None:
                try:
                    await asyncio.to_thread(self._event_store.append, envelope)
                except Exception as _recv_persist_exc:
                    log_event(
                        self.logger,
                        "event_store_receive_persist_failed",
                        level="warning",
                        topic=topic,
                        error_type=type(_recv_persist_exc).__name__,
                        error=str(_recv_persist_exc),
                    )

            # ── Phase 2: 提取 trace context + 在 span 内执行 handler ──
            # extract_trace_context 在没装 OTel 或 carrier 为空时返回 None；
            # 返回 None 时 start_span 创建新 trace root，链路仍然可追。
            # 设计文档：docs/task/stage_8_otel_integration_design.md §D4
            parent_ctx: Any = None
            if envelope.trace_context:
                try:
                    parent_ctx = extract_trace_context(envelope.trace_context)
                except Exception:
                    parent_ctx = None

            # R3-P1-X3：把 JetStream server-assigned sequence / num_delivered
            # 附到 message dict 上。handler 可基于 nats_metadata.stream_seq
            # 做单调性检查（per-subject 严格递增），num_delivered > 1 表明
            # redelivery，用于 handler 侧的幂等判断。失败走 best-effort：
            # nats-py 某些测试桩不暴露 metadata，取值失败 nats_metadata=None
            # 仍能继续处理。
            nats_metadata: dict[str, Any] | None = None
            try:
                _meta = getattr(msg, "metadata", None)
                if _meta is not None:
                    _seq = getattr(_meta, "sequence", None)
                    nats_metadata = {
                        "stream_seq": getattr(_seq, "stream", None) if _seq is not None else None,
                        "consumer_seq": getattr(_seq, "consumer", None) if _seq is not None else None,
                        "num_delivered": getattr(_meta, "num_delivered", None),
                        "timestamp": getattr(_meta, "timestamp", None),
                    }
            except Exception:
                nats_metadata = None

            message = {
                "topic": envelope.topic,
                "key": envelope.key,
                "payload": envelope.model_dump(mode="json"),
                "nats_metadata": nats_metadata,
            }

            # 绑定 parent context，使 start_span 开出的 span 成为子 span。
            token: Any = None
            _otel_detach: Any = None
            if parent_ctx is not None:
                try:
                    from opentelemetry.context import (  # type: ignore[import-not-found]
                        attach,
                        detach as _detach,
                    )
                    token = attach(parent_ctx)
                    _otel_detach = _detach
                except Exception:
                    token = None

            try:
                with start_span(
                    f"nats.receive.{topic}",
                    attributes={
                        "aats.topic": envelope.topic,
                        "aats.event_type": envelope.event_type,
                        "aats.event_id": envelope.event_id,
                        "aats.source_component": envelope.source_component,
                        "aats.consumer_role": self._consumer_role,
                        "aats.durable": durable,
                        "messaging.system": "nats",
                        "messaging.operation": "receive",
                    },
                ):
                    # handler 与 ack 分开捕获：区分 handler 业务错误和
                    # ack 网络错误，避免 ack 失败误报为 nats_handler_error。
                    # 两段都在 span 内确保 log_event 能拿到 trace_id。
                    try:
                        await handler(message)
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
                        # handler 失败 → nak 带 5s 延迟触发重投，给瞬时
                        # 故障留恢复窗口，避免立即耗尽 max_deliver。
                        # max_deliver 耗尽后消息不再投递给该 consumer
                        #（仍留在 stream 中直到 max_age / max_bytes 淘汰）。
                        try:
                            await msg.nak(delay=5)
                        except Exception:
                            # nats-py 旧版不支持 delay 参数时回退到无延迟
                            # nak，此时 server 会立即重投。
                            try:
                                await msg.nak()
                            except Exception:
                                pass
                    else:
                        try:
                            await msg.ack()
                        except Exception as exc:
                            log_event(
                                self.logger,
                                "nats_ack_failed",
                                level="warning",
                                topic=topic,
                                durable=durable,
                                error_type=type(exc).__name__,
                                error=str(exc),
                            )
                            # ack 失败但 handler 已成功执行：消息会在
                            # ack_wait 超时后被 server 重投。handler 的
                            # 幂等性保证重处理不会产生副作用。
            finally:
                if token is not None and _otel_detach is not None:
                    try:
                        _otel_detach(token)
                    except Exception:
                        pass

        # ── Slow consumer 防护：deliver_policy + flow_control ──────
        _DP_MAP = {
            "all": DeliverPolicy.ALL,
            "last": DeliverPolicy.LAST,
            "new": DeliverPolicy.NEW,
        }
        dp = _DP_MAP.get(spec.deliver_policy)
        if dp is None:
            raise ValueError(
                f"Unknown deliver_policy {spec.deliver_policy!r}; "
                f"expected one of: all, last, new"
            )

        consumer_config = ConsumerConfig(
            durable_name=spec.durable_name,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=spec.ack_wait_seconds,
            max_ack_pending=spec.max_ack_pending,
            max_deliver=spec.max_deliver,
            deliver_policy=dp,
            flow_control=spec.flow_control if spec.flow_control else None,
            idle_heartbeat=(
                spec.idle_heartbeat_seconds
                if spec.flow_control and spec.idle_heartbeat_seconds > 0
                else None
            ),
        )

        # Consumer 迁移：已有 durable consumer 的 deliver_policy 不匹配时，
        # NATS 返回 409 Conflict。捕获并删除旧 consumer 后重建。
        # 安全性：event topic 保持 deliver_policy=ALL，与已有 consumer 一致，
        # 不会触发迁移，不会丢失 ack 位置。
        stream_spec = self._config.stream_spec_for_topic(topic)
        stream_name = stream_spec.name if stream_spec else self._config.stream_name

        try:
            sub = await self._js.subscribe(
                subject=subject,
                durable=durable,
                cb=_on_msg,
                manual_ack=True,
                config=consumer_config,
            )
        except Exception as sub_exc:
            # 优先用 nats-py 结构化异常判断：err_code 10013 = consumer
            # already exists with different configuration。
            # fallback：字符串匹配兜底未来 nats-py 版本差异。
            _is_config_mismatch = (
                isinstance(sub_exc, BadRequestError)
                and getattr(sub_exc, "err_code", 0) == 10013
            )
            if not _is_config_mismatch:
                exc_str = str(sub_exc).lower()
                _is_config_mismatch = "consumer" in exc_str and any(
                    kw in exc_str
                    for kw in ("configuration", "deliver", "mismatch")
                )
            if _is_config_mismatch:
                log_event(
                    self.logger,
                    "nats_consumer_migration_needed",
                    level="warning",
                    topic=topic,
                    durable=durable,
                    stream=stream_name,
                    new_deliver_policy=spec.deliver_policy,
                    error=str(sub_exc),
                )
                try:
                    await self._js.delete_consumer(stream_name, durable)
                except Exception as del_exc:
                    log_event(
                        self.logger,
                        "nats_consumer_delete_failed",
                        level="error",
                        topic=topic,
                        durable=durable,
                        stream=stream_name,
                        error=str(del_exc),
                    )
                    raise del_exc from sub_exc
                log_event(
                    self.logger,
                    "nats_consumer_deleted_for_migration",
                    topic=topic,
                    durable=durable,
                    stream=stream_name,
                )
                sub = await self._js.subscribe(
                    subject=subject,
                    durable=durable,
                    cb=_on_msg,
                    manual_ack=True,
                    config=consumer_config,
                )
                log_event(
                    self.logger,
                    "nats_consumer_migrated",
                    topic=topic,
                    durable=durable,
                    stream=stream_name,
                    new_deliver_policy=spec.deliver_policy,
                )
            else:
                raise

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
            deliver_policy=spec.deliver_policy,
            flow_control=spec.flow_control,
            idle_heartbeat_seconds=spec.idle_heartbeat_seconds,
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
        """启动两条底层总线。

        slice nats-capacity 变更：
        -----------------------------
        之前实现会把 ``sorted(self._routing.critical_topics)`` 作为 topics
        参数传给 ``NatsEventBus.start(topics=...)``，走的是 legacy shim 路径，
        强制把所有 critical topic 挤进单条 ``AATS_EVENTS`` stream。这正是
        MARKET 高频流量能把整条 stream 撑爆 ``err_code=10023`` 的直接原因。

        现在 ``critical_start()`` **不再传 topics**，走的是 ``ensure_streams()``
        新路径，从 ``NatsBusConfig.streams``（由 ``_construct_event_bus``
        传入 ``build_nats_streams_from_env(DEFAULT_STREAM_SPECS)``）遍历完成
        多条 stream 的 upsert。分层后 MARKET 走独立短保留 stream，EVENTS 保留
        7 天长保留，互不挤占。

        这一层是为了让 build_runtime 调用方只需要 ``await bus.start()``
        即可，不必关心底层是 InMemoryEventBus（无 start）还是 NatsEventBus
        （需要 connect + ensure_streams）。

        设计文档：slice_nats_jetstream_capacity_fix_design.md §7.5a R2
        """
        critical_start = getattr(self._critical, "start", None)
        if critical_start is not None:
            # runtime 新路径：不传 topics，让 NatsEventBus.start() 走
            # ensure_streams() 遍历 self._config.streams
            await critical_start()
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
