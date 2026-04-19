# Loki + Promtail 模块审查报告

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


**审查日期**: 2026-04-13
**审查范围**: 日志采集管线（应用 JSON 日志 → Docker → Promtail → Loki → Grafana 面板 + 告警）
**基准**: Stage 9 dryrun checklist §5（告警覆盖）+ 项目可观测性架构需求

---

## 1. 审查范围与文件清单

### 日志生产层（应用侧）
| 文件 | 行数 | 说明 |
|------|------|------|
| `aats/bootstrap/logging.py` | 304 | JSON 格式化器、OTel trace 注入、log_event() API、文件轮转 |
| `aats/bootstrap/settings.py` | (部分) | log_dir / log_level / log_format / rotate 配置 |
| `tests/unit/test_logging_setup.py` | 301 | 21 个测试覆盖 JSON 格式、OTel 注入、log_event 端到端 |

### 日志采集层（Promtail）
| 文件 | 说明 |
|------|------|
| `deploy/wsl2-dev/promtail/promtail-config.yml` | Docker SD + JSON pipeline + label 提取 |
| `deploy/wsl2-dev/docker-compose.yml` (promtail 服务) | Promtail 3.0.0 容器定义 |

### 日志存储层（Loki）
| 文件 | 说明 |
|------|------|
| `deploy/wsl2-dev/loki/loki-config.yml` | TSDB + filesystem 存储、7 天保留、WAL、限流 |
| `deploy/wsl2-dev/docker-compose.yml` (loki 服务) | Loki 3.0.0 容器定义 |

### 日志消费层（Grafana）
| 文件 | 说明 |
|------|------|
| `deploy/wsl2-dev/grafana/provisioning/datasources/datasources.yml` | Loki 数据源 + trace_id → Jaeger derived field |
| `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/logs_overview.json` | 5 面板日志总览仪表盘 |
| `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/aats_operations.json` | 4 面板运维仪表盘（含 3 个 Loki 日志面板） |
| `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` | 5 条 Loki 告警规则（SEV1×2 + SEV2×2 + SEV3×1） |

### Docker Compose 环境配置
| 文件 | 说明 |
|------|------|
| `deploy/wsl2-dev/docker-compose.aats.yml` | 4 进程 + RDP 的 AATS_LOG_FORMAT=json / AATS_PROCESS_ROLE 设置 |

---

## 2. 端到端管线架构

```
┌─────────────────────────────────────────────────────────────────────┐
│ AATS 应用进程 (gateway / market / decision / execution)            │
│                                                                     │
│  logging.py::_JSONFormatter                                         │
│    → 单行 JSON: {timestamp, level, logger, message, process_role,  │
│                   trace_id, span_id, [event_name], [exception]}     │
│    → 输出到 stdout + RotatingFileHandler                            │
└────────────────────┬────────────────────────────────────────────────┘
                     │ Docker json-file log driver
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ /var/lib/docker/containers/<id>/*-json.log                         │
│  {"log":"{\"timestamp\":...}\n","stream":"stdout","time":"..."}    │
└────────────────────┬────────────────────────────────────────────────┘
                     │ Promtail Docker SD (refresh 5s)
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Promtail Pipeline                                                   │
│  Step 1: docker: {}           — 剥离 Docker 外层 JSON              │
│  Step 2: json: level, role    — 解析 AATS JSON 日志体               │
│  Step 3: labels: level, role  — 提升为 Loki 索引 label              │
│  relabel: job="aats", container=<name>                              │
└────────────────────┬────────────────────────────────────────────────┘
                     │ HTTP push → Loki /loki/api/v1/push
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Loki 3.0.0                                                          │
│  存储: TSDB + filesystem    保留: 168h (7天)    WAL: 启用           │
│  限流: 16MB/s ingestion     流数上限: 5000                          │
│  compactor: 2h 延迟删除                                             │
└────────────────────┬────────────────────────────────────────────────┘
                     │ LogQL
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Grafana                                                             │
│  仪表盘: logs_overview (5 面板) + aats_operations (3 Loki 面板)     │
│  告警: 5 条规则 (SEV1×2 + SEV2×2 + SEV3×1)                        │
│  derived field: trace_id → Jaeger 跳转                              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 需求检查清单

### 3.1 日志生产（应用侧）

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| A-1 | 结构化 JSON 日志 | ✅ PASS | `_JSONFormatter` 输出单行 JSON，`ensure_ascii=False` 支持中文 |
| A-2 | level 字段（大写） | ✅ PASS | `record.levelname` → DEBUG/INFO/WARNING/ERROR/CRITICAL |
| A-3 | process_role 字段 | ✅ PASS | 从 `AATS_PROCESS_ROLE` 环境变量读取，每条日志携带 |
| A-4 | trace_id / span_id | ✅ PASS | `_OTelTraceFilter` 注入，无 OTel 时降级为全零 |
| A-5 | event_name 结构化事件 | ✅ PASS | `log_event()` API 注入 event_name，高基数字段不做 label |
| A-6 | ISO 8601 时间戳 | ✅ PASS | `%Y-%m-%dT%H:%M:%S.%f` 毫秒精度 UTC 格式 |
| A-7 | exception 字段 | ✅ PASS | 仅在 `exc_info` 非空时出现，避免正常日志污染 |
| A-8 | 环境变量控制格式 | ✅ PASS | `AATS_LOG_FORMAT=json` 启用 JSON，默认 text |
| A-9 | 文件轮转 | ✅ PASS | RotatingFileHandler, 5MB/7 备份，分级目录 |
| A-10 | 第三方日志整合 | ✅ PASS | uvicorn/httpx/websockets propagate=True 统一格式 |

### 3.2 日志采集（Promtail）

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| B-1 | Docker 自动发现 | ✅ PASS | docker_sd_configs + unix socket, refresh 5s |
| B-2 | 容器过滤 | ✅ PASS | regex `/aats-.*` 只采集项目容器，排除宿主机无关容器 |
| B-3 | container label 提取 | ✅ PASS | relabel 去掉 Docker 前缀 `/`，得到可读容器名 |
| B-4 | job label 统一 | ✅ PASS | 硬编码 `job=aats`，所有 LogQL 查询的锚点 |
| B-5 | JSON 日志解析 | ✅ PASS | json stage 提取 level + process_role |
| B-6 | 非 JSON 日志兼容 | ✅ PASS | json stage 解析失败静默跳过，基础设施容器日志正常采集 |
| B-7 | label 基数控制 | ✅ PASS | 仅 3 个索引 label（container/level/process_role），高基数走正文搜索 |
| B-8 | Loki 推送配置 | ✅ PASS | `${LOKI_URL}` 环境变量，默认 `http://loki:3100/loki/api/v1/push` |
| B-9 | 依赖健康检查 | ✅ PASS | `depends_on: loki: condition: service_healthy` |
| B-10 | positions 持久化 | ⚠️ 见 L-1 | `/tmp/positions.yaml` 未挂载 volume |

### 3.3 日志存储（Loki）

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| C-1 | 存储引擎 | ✅ PASS | TSDB (schema v13) + filesystem，单节点开发配置 |
| C-2 | WAL 启用 | ✅ PASS | `wal.enabled: true`，crash 后数据不丢 |
| C-3 | 保留策略 | ✅ PASS | 168h (7天) + compactor 启用，开发环境足够 |
| C-4 | ingestion 限流 | ✅ PASS | 16MB/s rate + 32MB burst，4 进程日志量远低于此 |
| C-5 | stream 数上限 | ✅ PASS | 5000 per user，label 组合数远低于此 |
| C-6 | 自动标签注入禁用 | ✅ PASS | `discover_service_name: []` + `discover_log_levels: false` 避免误判 |
| C-7 | 旧样本拒绝 | ✅ PASS | `reject_old_samples: true`, max_age=24h |
| C-8 | 健康检查 | ✅ PASS | `wget /ready`, interval=15s, retries=5, start_period=30s |
| C-9 | 数据持久化 | ✅ PASS | `loki_data` named volume 挂载到 `/loki` |
| C-10 | 内存限制 | ✅ PASS | 512MB，单节点开发足够 |
| C-11 | 端口安全 | ✅ PASS | `127.0.0.1:3100:3100` 仅本地访问 |
| C-12 | 分析上报禁用 | ✅ PASS | `analytics.reporting_enabled: false` |

### 3.4 日志消费（Grafana 仪表盘）

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| D-1 | 日志量按级别 | ✅ PASS | `sum by (level) (count_over_time({job="aats"} [$__interval]))` |
| D-2 | 日志量按进程角色 | ✅ PASS | `sum by (process_role) (count_over_time(...))` |
| D-3 | 日志量按容器 | ✅ PASS | `sum by (container) (count_over_time(...))` |
| D-4 | 错误和警告过滤 | ✅ PASS | `level=~"ERROR\|WARNING\|CRITICAL"` |
| D-5 | 全日志可搜索 | ✅ PASS | process_role + container 双下拉筛选 |
| D-6 | 对账异常日志 | ✅ PASS | `\|= "reconciliation_mismatch"` 文本匹配 |
| D-7 | Kill Switch 事件 | ✅ PASS | `\|= "kill_switch"` 文本匹配 |
| D-8 | 模板变量绑定 | ✅ PASS | `label_values({job="aats"}, process_role/container)` |
| D-9 | Trace 跳转 | ✅ PASS | Loki derived field: trace_id regex → Jaeger datasource |
| D-10 | 双向 Trace-Log 关联 | ✅ PASS | Jaeger tracesToLogsV2 配置回查 Loki |

### 3.5 告警规则 (Stage 9 §5)

| # | 告警名称 | 级别 | noDataState | 状态 | 说明 |
|---|---------|------|-------------|------|------|
| R-1 | Kill Switch Triggered | SEV1 | OK | ✅ PASS | 5m 窗口 `\|= "kill_switch_applied"` |
| R-2 | Reconciliation Mismatch | SEV1 | OK | ✅ PASS | 15m 窗口 `\|= "reconciliation_mismatch"` |
| R-3 | Process Crash (Traceback) | SEV2 | OK | ✅ PASS | 5m 窗口 `\|= "Traceback"` |
| R-4 | Decision Cycle Stall | SEV2 | Alerting | ✅ PASS | 10m 窗口无 `decision_cycle_completed` → 触发 |
| R-5 | High Error Rate | SEV3 | OK | ✅ PASS | 15m 窗口 reduce→threshold>5 链式判定 |

### 3.6 测试覆盖

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| T-1 | JSON 格式化器 | ✅ PASS | 10 个测试：字段完整性、级别、时间戳、异常、事件名、中文 |
| T-2 | OTel trace 注入 | ✅ PASS | 4 个测试：always-pass、零值降级、ID 格式 |
| T-3 | log_event() API | ✅ PASS | 6 个测试：事件名、字段排序、级别路由、端到端 JSON |
| T-4 | 日志目录与轮转 | ✅ PASS | 1 个测试：5 级目录创建 + 级别分流 |

---

## 4. 发现项

### L-1 (LOW): Promtail positions 文件未持久化

**位置**: `deploy/wsl2-dev/promtail/promtail-config.yml` 第 22 行

```yaml
positions:
  filename: /tmp/positions.yaml
```

**问题**: positions 文件记录 Promtail 对每个容器日志文件的已读偏移量。当前路径 `/tmp/positions.yaml` 在容器内临时文件系统中，Promtail 服务的 volume 挂载列表不包含 `/tmp` 目录：

```yaml
volumes:
  - ./promtail/promtail-config.yml:/etc/promtail/promtail-config.yml:ro
  - /var/lib/docker/containers:/var/lib/docker/containers:ro
  - /var/run/docker.sock:/var/run/docker.sock:ro
  # ← 缺少 positions 持久化 volume
```

**影响**:
- 容器**重启**（`docker restart`）：positions 保留（容器文件系统不变）→ 无影响
- 容器**重建**（`docker compose down && up`）：positions 丢失 → Promtail 从头读取所有可用日志文件 → **日志重复推送到 Loki**
- 重复日志可能导致 SEV3 错误率告警误报（同一批 ERROR 日志被计数两次）
- Grafana 日志面板在重建时段出现重复条目

**修复方案**: 添加 named volume 持久化 positions 文件。

---

## 5. 审查结论

| 维度 | 评估 |
|------|------|
| 日志生产（JSON 格式化） | ✅ 生产就绪，字段与 Promtail pipeline 完全对齐 |
| OTel Trace 集成 | ✅ 无 OTel 时安全降级，有 OTel 时注入 trace/span ID |
| 结构化事件 API | ✅ log_event() 端到端测试通过，correlation 字段排序正确 |
| Promtail 采集管线 | ✅ Docker SD + 3 阶段 pipeline 正确，label 基数可控 |
| Loki 存储与限流 | ✅ TSDB + WAL + 7 天保留 + compactor，配置合理 |
| Grafana 仪表盘 | ✅ 2 个仪表盘 8 个 Loki 面板，覆盖日志量/错误/搜索 |
| 告警规则 | ✅ 5 条规则覆盖 SEV1-SEV3，noDataState 全部正确 |
| Trace-Log 双向关联 | ✅ Loki→Jaeger derived field + Jaeger→Loki tracesToLogsV2 |
| 单元测试 | ✅ 21 个测试覆盖格式化/注入/事件 API |
| positions 持久化 | ⚠️ L-1: 未挂载 volume，容器重建后日志重复 |

**总结**: Loki + Promtail 模块设计精良、端到端管线完整，**满足 Stage 9 dryrun 全部告警和可观测性需求**。唯一发现项 L-1（positions 未持久化）为低优先级部署配置问题，不影响日志采集的正确性和实时性，仅在容器重建场景产生重复日志。

---

## 6. 架构亮点

### 6.1 零侵入 Docker SD 模式
Promtail 不修改应用代码的日志输出路径——从 Docker 日志文件被动采集。应用只需输出到 stdout（Docker json-file driver 自动捕获），Promtail 通过 Docker Socket 发现容器、通过 `/var/lib/docker/containers` 读取日志文件。新增进程无需任何配置变更。

### 6.2 Label 基数精确控制
```
索引 label (低基数):  job=aats, container=<name>, level=<LEVEL>, process_role=<role>
正文搜索 (高基数):    trace_id, span_id, event_name, decision_id, symbol...
```
Loki 按 label 组合索引 stream，label 基数直接决定 stream 数量。当前设计：
- 14 个容器 × 5 个 level × 6 个 role ≈ 最大 ~420 streams，远低于 5000 上限
- trace_id 等高基数字段走 `| json | trace_id=...` 正文过滤，不产生 stream 膨胀

### 6.3 非 JSON 日志优雅降级
基础设施容器（aats-postgres、aats-redis、aats-nats 等）输出非 JSON 格式日志。Promtail 的 json stage 解析失败时**静默跳过**，日志仍然被采集（带 container label，无 level/process_role label）。Grafana 的 "Log Volume by Process Role" 面板用 `process_role=~".+"` 过滤器正确排除了这些无角色标签的基础设施日志。

### 6.4 全链路 Trace-Log 关联
```
Loki → Jaeger:  datasources.yml derivedFields matcherRegex 提取非零 trace_id
Jaeger → Loki:  tracesToLogsV2 按 traceId + process_role 反查日志
```
运维人员可在日志中点击 trace_id 跳转到 Jaeger trace 视图，也可在 Jaeger 中跳转到对应时间窗口的 Loki 日志。

---

*审查人: Claude (AI Audit)*
*审查方法: 全链路源码审读（logging.py → promtail-config.yml → loki-config.yml → Grafana JSON）+ LogQL 查询交叉验证 + Stage 9 告警规则逐条核对*
