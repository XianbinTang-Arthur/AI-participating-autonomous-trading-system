"""4-proc operator command 请求-响应代理（通用跨进程命令总线）。

设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md §4.4/§4.5

本模块提供两个类：

1. ``OperatorCommandClient``
   - 跑在 gateway 进程（HTTP endpoint 入口所在进程）
   - ``invoke(command, payload)`` 发请求 + 按 correlation_id 等响应
   - 构造期注入 ``process_role`` / ``bus`` / ``logger``，
     ``bootstrap()`` 订阅 response topic

2. ``OperatorCommandWorker``
   - 跑在目标业务进程（execution 驻 portfolio_service / reconciliation_service；
     decision 驻 ai_service）
   - 订阅 request topic，按 ``command`` 字段 dispatch 到注册过的 handler，
     直接复用 monolith 路径的业务逻辑（此时 runtime 所有字段都非 None）
   - 业务抛错时包成 ``success=False`` 的 Response 发回，不让异常漏出订阅 handler

两者的 request/response topic 通过构造参数注入，默认指向
``aats.system.operator_command_*``（execution 代理专用）；AI 代理复用本类、
构造时覆盖为 ``aats.system.ai_command_*``（decision 代理专用）。correlation_id
用 ``dict[str, asyncio.Future]`` 做请求-响应匹配。``component_name`` 参数
让 AI 链路的日志事件换前缀（``ai_command_*``）便于独立 grep / 告警。

设计原则：
    - Gateway 超时（默认 90 秒）会 fail HTTP handler；不做 retry（rebaseline
      幂等但 halt 不是，保守起见一律不 retry）
    - Worker 按请求 serial 处理（内部 asyncio.Lock），避免两个 rebaseline
      并发触发把 kill_switch / baseline 状态搅乱
    - source_role 标签用于避免自己发自己消费的回环（monolith 下本模块根本
      不装，不需要这层保护；但 4 进程下 worker 只在 execution / decision 装，
      client 只在 gateway 装，天然不会回环——这里仍记 role 是为了
      日志溯源 + 未来灵活性）
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.schemas.common import EventEnvelope, dump_payload_exact, new_id
from aats.schemas.operator_command import (
    OperatorCommandName,
    OperatorCommandRequest,
    OperatorCommandResponse,
)

# Event envelope 上的 event_type / source_component 常量。
_REQUEST_EVENT_TYPE = "OperatorCommandRequest"
_RESPONSE_EVENT_TYPE = "OperatorCommandResponse"
_SOURCE_COMPONENT = "aats.operator.command_bridge"

# 默认超时：90 秒。覆盖 rebaseline 最慢路径（OKX refresh + baseline import +
# reconciliation validate_now + auto-resume），留足余量避免 HTTP client 无限等待。
# 实测 rebaseline 含 auto-resume 在 OKX demo 上约 35-40 秒。可通过构造函数覆盖。
_DEFAULT_TIMEOUT_SECONDS = 90.0


class OperatorCommandError(RuntimeError):
    """代理链路本地错误（timeout / 未知命令 / client 未启动等）。"""


class OperatorCommandTimeoutError(OperatorCommandError):
    """Gateway 端 ``invoke()`` 等响应超时抛出。

    属性：
        correlation_id: 发出的请求 id，可用于人工查 NATS consumer / event_store
    """

    def __init__(self, correlation_id: str, timeout_seconds: float) -> None:
        super().__init__(
            f"operator_command_timeout: correlation_id={correlation_id}, "
            f"timeout={timeout_seconds}s"
        )
        self.correlation_id = correlation_id
        self.timeout_seconds = timeout_seconds


class OperatorCommandRemoteError(OperatorCommandError):
    """Execution 端业务抛错，通过 Response 包回来的。

    属性：
        error_type: 远端 exception 类名
        error_message: 远端 exception str
    """

    def __init__(self, error_type: str, error_message: str) -> None:
        super().__init__(
            f"operator_command_remote_error: {error_type}: {error_message}"
        )
        self.error_type = error_type
        self.error_message = error_message


# ─────────────────────────────────────────────────────────────────
# Gateway-side：OperatorCommandClient
# ─────────────────────────────────────────────────────────────────


class OperatorCommandClient:
    """Gateway 端请求方。

    使用姿势：
        client = OperatorCommandClient(bus=..., process_role="gateway", logger=...)
        await client.bootstrap()   # 订阅 response topic
        result = await client.invoke(command="rebaseline", payload={...})
        ...
        await client.stop()

    线程安全：``_pending`` dict 的读写全部在主 event loop 内发生
    （invoke/handle_response 都是 coroutine），不需要额外锁。
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        process_role: str,
        logger: Any,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        request_topic: str = topics.OPERATOR_COMMAND_REQUESTS,
        response_topic: str = topics.OPERATOR_COMMAND_RESPONSES,
        component_name: str = "operator_command",
    ) -> None:
        # request_topic / response_topic 默认指向 OPERATOR_COMMAND_*，保持
        # 现有 gateway↔execution 代理完全不变；AI 代理（gateway↔decision）
        # 构造时覆盖为 AI_COMMAND_* 即可复用同一套客户端逻辑。
        # component_name 作为日志 event 名前缀 + 结构化字段 ``component``，
        # 默认 "operator_command" 兼容既有调用，AI 链路传 "ai_command" 后
        # 日志会打成 ai_command_client_subscribed 等，便于独立 grep/告警。
        self._bus = bus
        self._process_role = process_role
        self._logger = logger
        self._timeout_seconds = timeout_seconds
        self._request_topic = request_topic
        self._response_topic = response_topic
        self._event_prefix = component_name
        self._pending: dict[str, asyncio.Future[OperatorCommandResponse]] = {}
        self._subscribed = False
        self._stopped = False

    async def bootstrap(self) -> None:
        """订阅 response topic。幂等：重复调用第二次后不会再 subscribe。

        在 ``build_runtime()`` 末尾、runtime 对外暴露前调用，确保后续任何
        ``invoke()`` 调用发生时订阅已就位。
        """
        if self._subscribed:
            return
        try:
            await self._bus.subscribe(
                self._response_topic,
                self._handle_response,
            )
            self._subscribed = True
            log_event(
                self._logger,
                f"{self._event_prefix}_client_subscribed",
                process_role=self._process_role,
                topic=self._response_topic,
            )
        except Exception as exc:
            log_event(
                self._logger,
                f"{self._event_prefix}_client_subscribe_failed",
                level="error",
                process_role=self._process_role,
                topic=self._response_topic,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

    async def invoke(
        self,
        *,
        command: OperatorCommandName,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """发请求 + 等响应。

        流程：
            1) 生成 correlation_id
            2) 在 _pending 里注册 future
            3) publish OperatorCommandRequest 到 NATS
            4) asyncio.wait_for(future, timeout)
            5) 成功 → 返回 result dict（与 monolith 路径完全一致）
            6) 失败 → 抛 OperatorCommandRemoteError
            7) 超时 → 抛 OperatorCommandTimeoutError

        Args:
            command: 命令名，必须是 OperatorCommandName Literal 里的值
            payload: 命令参数（reason / actor_role / actor_identity /
                auth_source）

        Returns:
            success 时 execution 端的业务返回 dict（rebaseline 的
            ``OperatorQueryService.rebaseline()`` return 结构）

        Raises:
            OperatorCommandError: client 未 bootstrap 或已 stop
            OperatorCommandTimeoutError: 超时
            OperatorCommandRemoteError: 远端业务抛错
        """
        if self._stopped:
            raise OperatorCommandError("operator_command_client_stopped")
        if not self._subscribed:
            raise OperatorCommandError(
                "operator_command_client_not_bootstrapped: "
                "call bootstrap() before invoke()"
            )

        correlation_id = new_id("opcmd")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OperatorCommandResponse] = loop.create_future()
        self._pending[correlation_id] = future

        request = OperatorCommandRequest(
            correlation_id=correlation_id,
            command=command,
            payload=dump_payload_exact(payload),
            requested_by_role=self._process_role,
        )

        # 把 request 塞进 EventEnvelope.payload，topic=OPERATOR_COMMAND_REQUESTS
        envelope = EventEnvelope(
            event_type=_REQUEST_EVENT_TYPE,
            source_component=_SOURCE_COMPONENT,
            topic=self._request_topic,
            key=correlation_id,
            payload=dump_payload_exact(request),
        )

        log_event(
            self._logger,
            f"{self._event_prefix}_request_publishing",
            process_role=self._process_role,
            correlation_id=correlation_id,
            command=command,
        )

        try:
            await self._bus.publish(
                topic=self._request_topic,
                key=correlation_id,
                payload=envelope.model_dump(mode="json"),
            )
        except Exception as exc:
            # publish 直接炸：清 future，抛原异常
            self._pending.pop(correlation_id, None)
            log_event(
                self._logger,
                f"{self._event_prefix}_request_publish_failed",
                level="error",
                process_role=self._process_role,
                correlation_id=correlation_id,
                command=command,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

        try:
            response = await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except asyncio.TimeoutError as exc:
            # finally 块统一清理 _pending，这里不重复 pop
            log_event(
                self._logger,
                f"{self._event_prefix}_request_timeout",
                level="error",
                process_role=self._process_role,
                correlation_id=correlation_id,
                command=command,
                timeout_seconds=self._timeout_seconds,
            )
            raise OperatorCommandTimeoutError(
                correlation_id=correlation_id,
                timeout_seconds=self._timeout_seconds,
            ) from exc
        except asyncio.CancelledError:
            log_event(
                self._logger,
                f"{self._event_prefix}_request_cancelled",
                level="warning",
                process_role=self._process_role,
                correlation_id=correlation_id,
                command=command,
            )
            raise
        finally:
            self._pending.pop(correlation_id, None)

        if not response.success:
            log_event(
                self._logger,
                f"{self._event_prefix}_request_remote_error",
                level="error",
                process_role=self._process_role,
                correlation_id=correlation_id,
                command=command,
                error_type=response.error_type,
                error_message=response.error_message,
            )
            raise OperatorCommandRemoteError(
                error_type=response.error_type or "RemoteError",
                error_message=response.error_message or "remote_error_no_message",
            )

        log_event(
            self._logger,
            f"{self._event_prefix}_request_succeeded",
            process_role=self._process_role,
            correlation_id=correlation_id,
            command=command,
            responder_role=response.responder_role,
        )
        return response.result or {}

    async def _handle_response(self, message: dict[str, Any]) -> None:
        """订阅 OPERATOR_COMMAND_RESPONSES 的 callback。

        按 correlation_id 查 _pending dict，resolve future。未知 id 记
        warning 后丢弃（可能是陈旧响应、或 client 侧 invoke 已超时把 entry
        清掉），不抛（订阅 handler 抛异常会让 NATS client 陷入 nak 重投循环）。
        """
        try:
            envelope = EventEnvelope.model_validate(message["payload"])
            response = OperatorCommandResponse.model_validate(envelope.payload)
        except Exception as exc:
            log_event(
                self._logger,
                f"{self._event_prefix}_response_parse_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        future = self._pending.get(response.correlation_id)
        if future is None:
            log_event(
                self._logger,
                f"{self._event_prefix}_response_unknown_correlation",
                level="warning",
                process_role=self._process_role,
                correlation_id=response.correlation_id,
                responder_role=response.responder_role,
            )
            return

        if future.done():
            # 已经超时或被其他消息 resolve 过了
            log_event(
                self._logger,
                f"{self._event_prefix}_response_future_already_done",
                level="warning",
                process_role=self._process_role,
                correlation_id=response.correlation_id,
            )
            return

        try:
            future.set_result(response)
        except asyncio.InvalidStateError:
            # 防守：done() 检查到 set_result 之间 future 状态可能已变
            # （asyncio 单线程下理论不会发生，但多一层防守无碍）
            pass

    async def stop(self) -> None:
        """关闭：取消所有未完成的 future（抛 OperatorCommandError）。

        EventBus 抽象不支持 unsubscribe，所以这里只清本地状态；下次进程
        重启会自然清理 NATS 端的订阅关系。
        """
        self._stopped = True
        for correlation_id, future in list(self._pending.items()):
            if not future.done():
                try:
                    future.set_exception(
                        OperatorCommandError(
                            f"operator_command_client_stopped_before_response:"
                            f"{correlation_id}"
                        )
                    )
                except asyncio.InvalidStateError:
                    pass
        self._pending.clear()
        log_event(
            self._logger,
            f"{self._event_prefix}_client_stopped",
            process_role=self._process_role,
        )


# ─────────────────────────────────────────────────────────────────
# Execution-side：OperatorCommandWorker
# ─────────────────────────────────────────────────────────────────


# Dispatcher 签名：接收 payload dict，返回 result dict（awaitable）。
CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class OperatorCommandWorker:
    """Execution 端请求处理方。

    构造时注入一个 dispatcher 映射（``command_handlers``），key 是命令名、
    value 是 async callable，接收 payload dict，返回业务 result dict。
    这样 bootstrap/config.py 就可以把 ``OperatorQueryService.rebaseline``
    / ``resume`` 包成 closure 注入进来，worker 本身不依赖任何业务类型。

    并发策略：
        所有命令的 dispatch 在 ``_lock`` 下 serial 执行。rebaseline / resume
        本身是系统状态切换操作，两个并发调用很危险（kill_switch / baseline
        顺序会乱），serial 化换正确性是合理 tradeoff。命令吞吐量极低
        （operator 手动触发），不会成为瓶颈。
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        process_role: str,
        logger: Any,
        command_handlers: dict[OperatorCommandName, CommandHandler],
        request_topic: str = topics.OPERATOR_COMMAND_REQUESTS,
        response_topic: str = topics.OPERATOR_COMMAND_RESPONSES,
        component_name: str = "operator_command",
    ) -> None:
        # request_topic / response_topic 默认指向 OPERATOR_COMMAND_*；
        # AI worker 构造时覆盖为 AI_COMMAND_* 以避免与 execution worker
        # 争抢同一条 NATS 订阅。component_name 作用同 Client：日志 event
        # 名前缀 + structured log 字段。
        self._bus = bus
        self._process_role = process_role
        self._logger = logger
        self._handlers = dict(command_handlers)
        self._request_topic = request_topic
        self._response_topic = response_topic
        self._event_prefix = component_name
        self._lock = asyncio.Lock()
        self._subscribed = False
        self._stopped = False
        # 去重集合：NATS ack_wait < handler 耗时时会重投同一条消息，
        # 已处理过的 correlation_id 直接跳过避免二次执行。
        # 用 OrderedDict 做 bounded LRU：operator 命令极低频（每小时
        # 个位数），2048 条足以覆盖数月运行，防止无界增长。
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._processed_ids_maxlen = 2048

    async def bootstrap(self) -> None:
        """订阅 request topic。幂等。

        在 ``build_runtime()`` 末尾调用，确保后续任何 gateway 发的请求都能
        被及时消费。
        """
        if self._subscribed:
            return
        try:
            await self._bus.subscribe(
                self._request_topic,
                self._handle_request,
            )
            self._subscribed = True
            log_event(
                self._logger,
                f"{self._event_prefix}_worker_subscribed",
                process_role=self._process_role,
                topic=self._request_topic,
                registered_commands=sorted(self._handlers.keys()),
            )
        except Exception as exc:
            log_event(
                self._logger,
                f"{self._event_prefix}_worker_subscribe_failed",
                level="error",
                process_role=self._process_role,
                topic=self._request_topic,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

    async def _handle_request(self, message: dict[str, Any]) -> None:
        """订阅 OPERATOR_COMMAND_REQUESTS 的 callback。

        Parse → dispatch（在 Lock 里）→ publish response。异常全在 worker
        内部处理，不漏到订阅 handler 之外。
        """
        if self._stopped:
            return
        try:
            envelope = EventEnvelope.model_validate(message["payload"])
            request = OperatorCommandRequest.model_validate(envelope.payload)
        except Exception as exc:
            log_event(
                self._logger,
                f"{self._event_prefix}_request_parse_failed",
                level="warning",
                process_role=self._process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        log_event(
            self._logger,
            f"{self._event_prefix}_request_received",
            process_role=self._process_role,
            correlation_id=request.correlation_id,
            command=request.command,
            requested_by_role=request.requested_by_role,
        )

        async with self._lock:
            # dedup check 必须在 lock 内：NATS ack_wait 重投可能在 lock
            # 等待期间到达，两条同 correlation_id 的消息都会通过 lock 外的
            # check，然后 serial 执行两次。lock 内 double-check 杜绝此竞态。
            if request.correlation_id in self._processed_ids:
                log_event(
                    self._logger,
                    f"{self._event_prefix}_request_deduplicated",
                    level="warning",
                    process_role=self._process_role,
                    correlation_id=request.correlation_id,
                    command=request.command,
                )
                return

            response = await self._dispatch(request)

            # dispatch 成功后标记为已处理。_dispatch 内部 try/except 兜底
            # 不会漏出异常，所以这里总会执行。
            self._processed_ids[request.correlation_id] = None
            while len(self._processed_ids) > self._processed_ids_maxlen:
                self._processed_ids.popitem(last=False)

        # publish 响应（best-effort：响应发不出去 caller 会超时，我们不能把
        # 失败当业务错误——业务已经执行完了——所以最好只打日志让 ops 排查）
        try:
            response_envelope = EventEnvelope(
                event_type=_RESPONSE_EVENT_TYPE,
                source_component=_SOURCE_COMPONENT,
                topic=self._response_topic,
                key=response.correlation_id,
                payload=dump_payload_exact(response),
            )
            await self._bus.publish(
                topic=self._response_topic,
                key=response.correlation_id,
                payload=response_envelope.model_dump(mode="json"),
            )
            log_event(
                self._logger,
                f"{self._event_prefix}_response_published",
                process_role=self._process_role,
                correlation_id=response.correlation_id,
                command=request.command,
                success=response.success,
            )
        except Exception as exc:
            log_event(
                self._logger,
                f"{self._event_prefix}_response_publish_failed",
                level="error",
                process_role=self._process_role,
                correlation_id=response.correlation_id,
                command=request.command,
                response_success=response.success,
                response_error_type=response.error_type,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _dispatch(
        self,
        request: OperatorCommandRequest,
    ) -> OperatorCommandResponse:
        """按 command 字段找 handler 并执行，包成 Response。

        未注册命令 → success=False，error_type=UnknownCommandError
        Handler 抛异常 → success=False，error_type + error_message 来自
            原异常。业务 error 走这条路径（例如 rebaseline 抛
            "rebaseline_requires_okx_account_read"）。
        """
        handler = self._handlers.get(request.command)
        if handler is None:
            return OperatorCommandResponse(
                correlation_id=request.correlation_id,
                success=False,
                error_type="UnknownCommandError",
                error_message=f"unknown_command:{request.command}",
                responder_role=self._process_role,
            )
        try:
            result = await handler(request.payload)
        except Exception as exc:
            # 业务校验异常（ValueError/KeyError/RuntimeError）用 info 级别，
            # 系统异常（网络/IO/未预期）用 error 级别，便于 ops 区分告警。
            is_business_error = isinstance(exc, (ValueError, KeyError, RuntimeError))
            log_event(
                self._logger,
                f"{self._event_prefix}_handler_raised",
                level="info" if is_business_error else "error",
                process_role=self._process_role,
                correlation_id=request.correlation_id,
                command=request.command,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return OperatorCommandResponse(
                correlation_id=request.correlation_id,
                success=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
                responder_role=self._process_role,
            )
        if not isinstance(result, dict):
            log_event(
                self._logger,
                f"{self._event_prefix}_handler_non_dict_result",
                level="warning",
                process_role=self._process_role,
                correlation_id=request.correlation_id,
                command=request.command,
                result_type=type(result).__name__,
            )
        return OperatorCommandResponse(
            correlation_id=request.correlation_id,
            success=True,
            result=dump_payload_exact(result) if isinstance(result, dict) else {},
            responder_role=self._process_role,
        )

    async def stop(self) -> None:
        """关闭。不取消 in-flight dispatch（_lock 会让它们自然跑完）。"""
        self._stopped = True
        log_event(
            self._logger,
            f"{self._event_prefix}_worker_stopped",
            process_role=self._process_role,
        )
