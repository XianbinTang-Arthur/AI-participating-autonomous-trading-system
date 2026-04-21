# scripts/diag/ — 生产诊断工具集

复用类诊断脚本合集。设计原则：
- **只读**，从不改状态
- **幂等**，可任意次运行
- **只用 docker exec**，不读凭证文件
- **短小**，每个脚本聚焦一个诊断维度

## 工具清单

| 脚本 | 用途 | 典型场景 |
|------|------|----------|
| `pg_connection_health.sh` | 采样 Postgres 连接状态分布（active / idle / idle_in_tx） | UI 卡、pool 耗尽怀疑 |
| `pg_full_scan_audit.sh` | 找当前是否有无 WHERE + 大表 + JSONB 的 SELECT active | 确认 full-scan 未复发 |
| `gateway_slow_queries.sh` | 汇总 `parallel_fetch_slow` 日志 top-N wall time | gateway API 慢 |
| `recovery_rollback_gap.sh` | 实时采样 idle-in-tx 的 `state_change → now` gap | 判断 `session.close() → rollback` 延迟基线 |

## 使用

在项目根目录直接跑：

```bash
bash scripts/diag/pg_connection_health.sh
bash scripts/diag/pg_full_scan_audit.sh
bash scripts/diag/gateway_slow_queries.sh 10  # top 10
bash scripts/diag/recovery_rollback_gap.sh 30  # 30 ticks
```

所有脚本假设：
- `wsl -d Ubuntu` 可达
- `aats-postgres` / `aats-gateway` 容器运行中

## 来源

2026-04-21 autonomous session 抓"生产 UI 卡"时陆续写的 one-off 脚本整合。
