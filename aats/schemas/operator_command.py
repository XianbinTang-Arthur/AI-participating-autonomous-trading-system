"""OperatorCommand 请求 / 响应 schema。

Slice 4-proc operator command proxy 设计文档：
docs/task/slice_4proc_operator_command_proxy_fix_design.md §4.3

设计意图：
    gateway 进程的 HTTP endpoint（POST /system/rebaseline、/system/resume）
    需要访问 portfolio_service / reconciliation_service，但 4 进程切片化
    门控让这两个 service 只在 execution role 装配。本 schema 定义了通过
    NATS 从 gateway 把命令代理到 execution 进程的请求-响应消息格式。

correlation_id 规则：
    - gateway 端 OperatorCommandClient 在发请求前生成 ``new_id("opcmd")``
    - Request / Response 两条消息的 correlation_id 必须完全相同
    - Client 收到 Response 时按 correlation_id 查本地 future dict 进行路由
    - Response 上的 correlation_id 在 Client 侧找不到对应 future 时记
      warning 后丢弃（避免陈旧响应污染后续请求）

payload 结构不做强类型约束：
    不同命令的参数结构差异大（rebaseline 只需要 reason + actor_*，resume
    多一层 blocker 检查参数），不值得为每个命令定义独立 schema。payload
    作为 dict[str, Any] 透传，由 Worker 端的 dispatcher 按 command 字段
    路由到对应的本地方法，参数解构在本地方法入口处发生。

不做的事：
    - 不引入 async iterator / chunked response（rebaseline 响应 ~几 KB，
      一次性传完）
    - 不做 retry / idempotent key（rebaseline 本身是幂等的：读当前 OKX
      状态 + 写新 baseline；即便 client 超时重试，execution 收到后只是
      多产生一个新 baseline event，不破坏数据一致性）
    - 不做消息压缩（低频 operator 操作，每秒最多 1-2 条）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, utc_now

# 支持的命令枚举。
#
# 新增命令时必须：
#   1) 在本 Literal 里加名称
#   2) 在 ``OperatorCommandWorker._COMMAND_HANDLERS`` 注册 dispatcher
#   3) 在 ``reconciliation_system_queries`` 对应方法的 gateway 分支里
#      改 client.invoke 的 command 参数
#   4) 在 test_operator_command_bridge 加 unit test 覆盖新命令
OperatorCommandName = Literal[
    "rebaseline",
    "resume",
    "validate_reconciliation",
    "cancel_order",
    "resolve_stuck_submission",
    "refresh_exchange_state",
    "retry_limit_lookup",
    "safe_cancel_exit_execution",
    "reset_trial_guard",
]


class OperatorCommandRequest(SchemaBase):
    """Gateway → Execution 请求消息。

    字段：
        correlation_id: 请求唯一 id；Response 必须回相同值。
        command: 要执行的命令名，必须在 OperatorCommandName 里。
        payload: 命令参数 dict。按约定至少包含 reason / actor_role /
            actor_identity / auth_source 四个字段以便 execution 侧的
            本地方法能直接解构。
        requested_at: gateway 发请求时刻（UTC）。
        requested_by_role: 发请求的进程 role（通常是 "gateway"，monolith
            路径不会走代理，所以不会出现 "monolith"；execution 自己代理
            自己属于错误路径，会被 Worker 拒绝）。
    """

    correlation_id: str
    command: OperatorCommandName
    payload: dict[str, Any]
    requested_at: datetime = Field(default_factory=utc_now)
    requested_by_role: str


class OperatorCommandResponse(SchemaBase):
    """Execution → Gateway 响应消息。

    字段：
        correlation_id: 回 Request 的同一个 id。Client 按这个字段路由。
        success: 业务是否成功。True 时 ``result`` 必须非 None。
        result: 成功时的业务返回 dict（与 monolith 路径的
            ``OperatorQueryService.rebaseline()`` 返回值结构完全一致）。
            失败时为 None。
        error_type: 失败时的 exception 类名（``type(exc).__name__``）。
            成功时为 None。
        error_message: 失败时的 exception str。成功时为 None。
        responded_at: Worker 发响应时刻（UTC）。
        responder_role: 发响应的进程 role（通常是 "execution"）。
    """

    correlation_id: str
    success: bool
    result: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    responded_at: datetime = Field(default_factory=utc_now)
    responder_role: str
