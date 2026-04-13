# Grafana 审计修复设计

## 背景

Grafana 模块审计发现 7 个问题（G-1 ~ G-7），核心矛盾是**基础设施层完好但应用层空白**：
- 指标管线断裂（MetricsRegistry → Grafana 不通）
- Stage 9 必须的 dashboard 面板全部缺失
- 告警系统完全空白

## 关键设计决策

### D1: 指标管线方案——OTel Bridge + Prometheus Server

**选型**：MetricsRegistry → OTel Counter (bridge) → PrometheusMetricReader :9464 → Prometheus server → Grafana

**否决方案**：
- Loki log-based metrics：`rate()` / `sum()` 慢且受限，不适合 fill success rate 等复合指标
- Postgres 写入：增加写负载和延迟
- 直接 JSON HTTP：Grafana 无法查询 REST JSON endpoint

**实现**：新建 `aats/bootstrap/metrics_bridge.py`，定期同步 MetricsRegistry.snapshot()
的增量到 OTel Counter。新增 Prometheus 容器采集 4 个 AATS 进程的 :9464 端口。

### D2: Dashboard 面板——单 aats_operations.json 覆盖 Stage 9 §3.1

一个 dashboard 涵盖：进程心跳（Loki）、决策周期/成交率（Prometheus）、
Postgres 连接池（Postgres SQL）、错误日志/对账异常（Loki）。

Redis latency p99 暂用 Loki 查询 hot_state_store 连接事件，精确延迟仪表化
推迟到后续 Stage。

### D3: 告警规则——5 条 Loki-based 最小可行告警

| 规则 | 严重度 | 数据源 |
|------|--------|--------|
| kill_switch 触发 | SEV1 | Loki |
| 对账不一致 | SEV1 | Loki |
| 进程崩溃 (Traceback) | SEV2 | Loki |
| 决策周期停滞 | SEV2 | Loki |
| 错误率过高 | SEV3 | Loki |

Contact point 用 Grafana 内建日志（Stage 10 再接 Slack/email）。

### D4: DriftReport 暴露

AbortHookService 缓存最后一次 DriftReport，新增 `/system/drift-report` HTTP endpoint。

## 文件变更清单

| 文件 | 变更类型 |
|------|----------|
| `deploy/wsl2-dev/grafana/provisioning/datasources/datasources.yml` | 修改：加 uid、加 Prometheus 数据源 |
| `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/logs_overview.json` | 修改：修 uid、修 regex |
| `deploy/wsl2-dev/docker-compose.yml` | 修改：加 Prometheus 服务 |
| `deploy/wsl2-dev/prometheus/prometheus.yml` | 新增：Prometheus 采集配置 |
| `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` | 新增：5 条告警规则 |
| `aats/bootstrap/metrics_bridge.py` | 新增：MetricsRegistry → OTel Counter 桥接 |
| `aats/services/governance_engine/abort_hooks.py` | 修改：缓存 DriftReport |
| `aats/api/routes.py` | 修改：新增 /system/drift-report |
| `tests/unit/test_metrics_bridge.py` | 新增：桥接模块单元测试 |
