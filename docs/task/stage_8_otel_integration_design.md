# Stage 8：OpenTelemetry + Jaeger 端到端分布式追踪集成

## 1 背景

Stage 6 把 monolith 拆成 gateway / market / decision / execution 4 进程后，
一条交易意图会穿过全部 4 个进程：

```
gateway (HTTP request)
  → NATS (policy.decisions, strategy.decision_context, …)
    → market (features.snapshots)
      → NATS
        → decision (strategy.decision_outcome, policy.decisions)
          → NATS (execution.order_intents)
            → execution (execution.plans, execution.order_updates, execution.fill_events)
              → exchange
                → reconciliation / portfolio → snapshot_cache
```

拆分之前 monolith 的调用栈可以靠 `log_event(...)` + grep `decision_id` 串起来。
拆分之后 `decision_id` 虽然还在 payload 里，但：

1. **时间序列错位**：4 个进程的时钟可能漂移，grep 出的事件很难按 wall-clock 排序
2. **异常扩散不可见**：decision_proc 一个内部异常可能要 10s 才在 execution_proc
   的 fill_events 上显现出"订单没 fill"；中间 9.9s 的黑盒全靠想象
3. **瓶颈定位**：Stage 9 dryrun 之后真金白银开跑，如果发现某条 decision 的
   end-to-end P99 延迟从 200ms 抖到 2s，没有 trace 等于盲人摸象

OTel + Jaeger 是业界标准解法：

- OTel SDK 负责生成 span + 通过 OTLP 推到 collector
- Jaeger 作为 all-in-one collector + storage + UI
- W3C TraceContext（`traceparent` header）把 trace_id 和 parent span_id
  通过事件边界传递，所有子 span 自动挂在同一棵树上

## 2 现状清点

### 2.1 已就位
- `aats/bootstrap/telemetry.py`：`TelemetryConfig` / `configure_telemetry` /
  `start_span` / `inject_trace_context` / `extract_trace_context` 全套骨架，
  带 `_NoopTracer` 降级，OTel 包没装也能 import 不抛错
- `tests/unit/test_telemetry_skeleton.py`：17 个单元测试覆盖 no-op 路径
- `deploy/wsl2-dev/docker-compose.yml` §jaeger：jaegertracing/all-in-one:1.57
  容器已经常驻，暴露 16686 UI / 4317 OTLP gRPC / 4318 OTLP HTTP，badger
  做本地存储，已验证 healthy
- `deploy/wsl2-dev/grafana/provisioning/datasources/`：Grafana 已配 Jaeger
  datasource，可以用 trace_id 从 Loki 直接跳到 Jaeger span tree

### 2.2 缺口
- `pyproject.toml` 没有 `opentelemetry-*` 依赖
- `deploy/wsl2-dev/Dockerfile` 没有装 OTel wheel
- `build_runtime` 没有调用 `configure_telemetry`；4 进程启动时 tracer 是 no-op
- `EventEnvelope` schema 里没有 trace_context 字段；跨进程事件穿越 NATS 时
  trace 链路断掉
- `NatsEventBus.publish_envelope` / `subscribe` 没有 inject/extract hook
- 关键业务路径（gateway API handler、decision cycle、execution submit、
  portfolio snapshot save）没有 `start_span` 调用
- Jaeger UI 里没有任何 trace（因为 4 进程都在 no-op 模式下跑）

## 3 目标与非目标

### 3.1 目标（Stage 8 收尾条件）
1. 4 个 AATS 进程启动时各自调一次 `configure_telemetry`，service_name 与
   process_role 对齐（aats-gateway / aats-market / aats-decision /
   aats-execution）
2. `EventEnvelope.trace_context: dict[str, str] | None = None` 字段新增，
   默认 None，向后兼容历史事件
3. `NatsEventBus.publish_envelope` 在 publish 前自动 `inject_trace_context`
   到 `envelope.trace_context`
4. `NatsEventBus.subscribe` 的 `_on_msg` 在调用 handler 前把 envelope 的
   `trace_context` 用 `extract_trace_context` 还原成 OTel context，并 attach
   到当前 task 的 current span stack（使得 handler 内部的 `start_span` 会
   自动 parent 到上游的 span）
5. 关键业务路径加 span：
   - `gateway`: FastAPI handler 级别的 `/system/*` 与 `/decision/*` 路由
   - `decision`: `DecisionEngine.run_cycle` 外层 span + 每个 strategy 的内
     层 span
   - `execution`: `ExecutionCoordinator.submit_intent` + OKX adapter
     `submit_order` 的 HTTP 边界 span
   - `portfolio`: `portfolio_repo.save_snapshot` + `cache.publish`
6. WSL2 4 进程真跑一次 halt→resume 或一轮决策周期（sandbox 模式避免真金
   白银），在 Jaeger UI `http://127.0.0.1:16686` 里看到完整的 trace tree，
   至少覆盖 gateway → decision → execution 三级 parent-child 关系
7. 所有新增 unit test + 既有 test_telemetry_skeleton 17 测全通
8. runbook 新增 §9.5 "OTel / Jaeger 真跑 drill"

### 3.2 非目标（明确不做）
- 不给 OTel span 加 log sampling／rate limiting（dev 模式 100% 采样，
  生产调优留到 Stage 10）
- 不做 metrics（prometheus-python-client）或 logs 转 OTLP（Loki 路径已经
  工作，双轨太重）
- 不做 auto-instrumentation（`opentelemetry-instrumentation-fastapi` 等
  会引入一堆依赖 + 性能未知 + 与我们自己的 `log_event` 模式冲突，手写
  span 更可控）
- 不做 collector sidecar（jaeger all-in-one 已经是 OTLP collector）
- 不做 cross-org trace export（仅限 WSL2 dev + 未来自建 prod jaeger）

## 4 设计决策

### D1 — OTel 依赖是 optional extra

**决策**：在 `pyproject.toml` 新增 `[project.optional-dependencies].otel`，
不进 main `dependencies`。

**理由**：
- 单测 / CI 不依赖真实 OTel，用 no-op tracer 覆盖即可
- OTel SDK + exporter + proto 一共会拖入 ≥8 个 wheel（大约 +40MB），
  monolith 开发者不一定愿意装
- dockerfile 在 build 时 `pip install -e .[otel]` 显式装，保证容器里 always on
- `telemetry.py` 已经有 `ImportError` 保护，没装 OTel 不会挂

**实现**：
```toml
[project.optional-dependencies]
otel = [
  "opentelemetry-api>=1.27",
  "opentelemetry-sdk>=1.27",
  "opentelemetry-exporter-otlp-proto-grpc>=1.27",
  "opentelemetry-semantic-conventions>=0.48b0",
]
```

### D2 — configure_telemetry 由 build_runtime 在顶层调用一次

**决策**：在 `aats/bootstrap/config.py` 的 `build_runtime` 开头，logging
初始化之后、settings_provenance 之前，调用 `configure_telemetry(cfg)`。

**理由**：
- `build_runtime` 是所有 4 个进程的共用入口（via uvicorn main + entrypoint
  script），只要在这里配一次就全覆盖
- 早一点配好，后面的任何 `start_span` / `log_event` 都能用
- 失败了也不能抛错——telemetry 可观测性降级不应阻塞交易

**配置**：
- `config.service_name = f"aats-{effective_process_role}"`
- `config.otlp_endpoint = os.environ.get("AATS_OTEL_ENDPOINT", "http://jaeger:4317")`
  （容器内部访问走 service 名 `jaeger`，不走 `127.0.0.1`）
- `config.deployment_environment = os.environ.get("AATS_OTEL_DEPLOYMENT_ENV", "dev")`

**bootstrap log 断言（加入 runbook §9.1 冒烟清单）**：
```
telemetry_configured service_name=aats-<role> process_role=<role>
  endpoint=http://jaeger:4317 protocol=grpc sample_ratio=1.0
```

### D3 — EventEnvelope 加 trace_context 字段（不加 meta 容器）

**决策**：`EventEnvelope` 直接加 `trace_context: dict[str, str] | None = None`
字段，不引入 meta / headers 子容器。

**理由**：
- Pydantic default None → 历史 persist 的 JSON 反序列化自动补 None，向后
  兼容到位
- 不需要 schema migration
- trace_context dict 最多 2-3 个 key（traceparent / tracestate），直接平铺
  比嵌套 meta 更简单
- 未来如果要加 baggage / correlation_id 再考虑升级到 meta

**schema**：
```python
class EventEnvelope(SchemaBase):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    event_type: str
    event_timestamp: datetime = Field(default_factory=utc_now)
    source_component: str
    topic: str
    key: str
    payload: dict[str, Any]
    trace_context: dict[str, str] | None = None  # ← Stage 8 新增
```

**测试**：
- 旧 JSON（没有 trace_context 字段）反序列化 → `trace_context is None`
- 新 JSON 带 traceparent → `trace_context == {"traceparent": "..."}`
- model_dump_json 默认包含字段（None 也序列化）

### D4 — NatsEventBus 在 publish/subscribe 边界 inject/extract

**决策**：
- `publish_envelope` 在 persist 后、JetStream publish 前，如果
  `envelope.trace_context is None`，调 `inject_trace_context(envelope.trace_context := {})`
  填入当前 span 的 traceparent
- `subscribe._on_msg` 在 `EventEnvelope.model_validate(payload_dict)` 之后、
  `await handler(message)` 之前，用 `extract_trace_context(envelope.trace_context)`
  还原 context，然后用 `opentelemetry.context.attach(ctx)` / `detach(token)`
  包裹 handler 调用

**理由**：
- 把 trace 传播放在 bus 层，业务代码完全透明
- publish 时如果上游没有 active span，`inject` 是 no-op，trace_context 保持
  `{}` 或不写入，下游 extract 也无影响
- subscribe 时如果 envelope 没有 trace_context，`extract` 返回 OTel 空 context，
  handler 内部 `start_span` 会开启一个新 root span（可接受）

**边界情况**：
- OTel 未装 → inject/extract 是 no-op → trace_context 保持 None → 行为
  等同于 Stage 7
- envelope.trace_context 被下游 handler 再次 publish 时需要 **重新 inject**
  （因为 handler 内部的 `start_span` 已经产生了新的 child span），不要直接透传
  上游的 trace_context

**实现放在 NatsEventBus 里而不是 HybridEventBus 里**：
- HybridEventBus 对 critical topics 走 NATS，non-critical 走 InMemoryEventBus
- InMemoryEventBus 是同一进程内 fire-and-forget，OTel 的 `current span` 已经
  通过 asyncio context 自动传播，不需要 inject/extract
- 所以 trace 边界就是 NATS 出入口，放在 NatsEventBus 层最精准

### D5 — 关键业务路径 span 命名规范

**命名约定**：`{process_role}.{module}.{action}`

| 位置 | span 名 | 关键 attribute |
|------|---------|----------------|
| gateway FastAPI `/system/*` | `gateway.api.system_{action}` | http.status_code, operator.user |
| gateway FastAPI `/decision/*` | `gateway.api.decision_{action}` | decision_id |
| decision DecisionEngine.run_cycle | `decision.engine.run_cycle` | decision_id, symbol, scope_fingerprint |
| decision strategy inner loop | `decision.strategy.{strategy_id}` | strategy_id, confidence |
| execution submit_intent | `execution.coordinator.submit_intent` | client_order_id, intent_type |
| execution OKX adapter submit | `execution.okx.submit_order` | client_order_id, exchange_order_id |
| portfolio save_snapshot | `portfolio.repo.save_snapshot` | decision_id, total_equity |
| reconciliation validate | `reconciliation.engine.validate` | scope_fingerprint |

**attribute 命名**：遵循 `aats.*` 前缀，不污染 OTel semantic conventions。

### D6 — WSL2 真跑 drill 步骤

**drill 路径**：
1. WSL2 `docker compose` 重建镜像（需要装 OTel wheel）
2. 4 进程启动后 bootstrap log 必须有 `telemetry_configured` × 4
3. `curl -X GET http://127.0.0.1:8000/healthz` 产生一个 trace
4. 等 5s（BatchSpanProcessor 的 schedule_delay），浏览器打开
   `http://127.0.0.1:16686/search`，service dropdown 选 `aats-gateway`
5. 应该能看到一条 trace，含 1 个 `gateway.api.healthz` span
6. 触发一次 halt/resume drill（§9.2 现有步骤）；由于 halt/resume 涉及 4
   进程 NATS 广播，应该看到跨 4 个 service 的 trace tree
7. 截图 Jaeger UI 存档到 `docs/task/stage_8_otel_trace_screenshot.png`
8. runbook §9.5 固化上面这套流程

## 5 实施切片

| 切片 | 范围 | 产物 | 独立 commit |
|------|------|------|-------------|
| 8-2 | pyproject + Dockerfile + build_runtime 接入 configure_telemetry | 1 个 commit | ✅ |
| 8-3 | EventEnvelope trace_context + NatsEventBus inject/extract + 单测 | 1 个 commit | ✅ |
| 8-4 | 关键路径 start_span 批量接入（一次 commit，按模块分节） | 1 个 commit | ✅ |
| 8-5 | (合到 8-2 commit 的 dockerfile 一起) | — | 合并 |
| 8-6 | WSL2 重建 + drill + runbook §9.5 + 截图 | 1 个 commit | ✅ |

每个切片跑完 unit + 相关 integration 后再 commit，保证 main 任何提交都能
build + test 通过。

## 6 回滚策略

- 所有 OTel 相关代码都有 `try/except ImportError` 兜底
- `configure_telemetry` 失败时 tracer 降级为 `_NoopTracer`，业务路径继续跑
- 回滚时只需要 `pip uninstall opentelemetry-*`（`.otel` extra）或设置
  `AATS_OTEL_ENDPOINT=` 空字符串，span 生产路径仍然是 no-op
- EventEnvelope.trace_context 默认 None，下游兼容
- Jaeger 容器可独立停掉：`docker compose stop jaeger`，OTLP exporter
  retries 失败后 BatchSpanProcessor 会 drop 旧 batch，不影响业务

## 7 风险 & 缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| OTel SDK 在容器内和单测环境版本不一致 | span 属性解析异常 | pyproject.toml 写死版本下限 ≥1.27 |
| BatchSpanProcessor 后台线程在进程 shutdown 时泄漏 | 日志脏 | build_runtime 注册 atexit hook 调 shutdown_telemetry |
| trace_context 在 strict mode event_store 持久化时多占空间 | 磁盘慢增长 | single span trace 带 2 key 的 dict ≈ 120 bytes，可忽略 |
| 单元测试的 `EventEnvelope.model_dump_json()` 断言因新字段 fail | 回归 | 所有旧断言已按字段名明检，增字段不影响；但要跑一遍 pytest 确认 |
| OTel instrumentation 自身的 CPU 开销影响热路径延迟 | 低 | 本次只做手写 span，密度可控；Stage 9 dryrun 先跑 sample_ratio=1.0，真金白银之前降到 0.1 |

## 8 验收标准

- [ ] 单元测试：`pytest tests/unit/test_telemetry_skeleton.py tests/unit/test_nats_bus*` 全通
- [ ] 单元测试新增：EventEnvelope trace_context roundtrip
- [ ] 单元测试新增：NatsEventBus publish/subscribe 用 no-op tracer 不抛错
- [ ] WSL2 rebuild 后 4 个容器都打印 `telemetry_configured process_role=<role>`
- [ ] Jaeger UI `http://127.0.0.1:16686/search` 能查到 `aats-gateway`、
      `aats-decision`、`aats-execution` 至少 3 个 service
- [ ] 一次 halt/resume drill 产生的 trace 能在 Jaeger UI 查到，且有至少
      2 层 parent-child（gateway → NATS → decision）
- [ ] runbook §9.5 更新完毕

## 9 参考

- W3C TraceContext: https://www.w3.org/TR/trace-context/
- OTel Python SDK: https://opentelemetry.io/docs/languages/python/
- Jaeger all-in-one docker: https://www.jaegertracing.io/docs/deployment/#all-in-one
- 现有 telemetry 骨架：`aats/bootstrap/telemetry.py`
- 4 进程拓扑 runbook：`deploy/wsl2-dev/RUNBOOK.md` §9
