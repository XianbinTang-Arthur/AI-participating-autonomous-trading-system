# 13 资金安全威胁模型与核心不变量

## 资产

- 交易所资金、保证金、现货资产和可用额度。
- API key/secret/passphrase、operator session/API key、数据库与消息总线凭证。
- 订单、成交、仓位、lots、ledger、PnL、费用、funding、reconciliation 事实。
- kill switch、recovery、risk limits、active parameters、strategy rollout 与人工审批状态。
- 研究数据、回测证据、候选推荐和审计日志的真实性。

## 信任边界

```text
Operator browser
  <-> host network / TLS boundary
  <-> Gateway auth + control plane
  <-> Redis/NATS/PostgreSQL internal network
  <-> market / decision / execution processes
  <-> public internet
  <-> OKX public/private APIs

RDP collectors/research
  <-> research database and artifacts
  <-> governance approval/apply/rollback
  <-> runtime active parameters
```

攻击者既包括外部网络攻击者，也包括错误配置、失效第三方服务、有凭证的低权限用户、被污染依赖、异常市场数据、重复/迟到消息和操作员误判。资金系统中“故障”与“攻击”可产生相同后果，必须共享安全控制。

## 主要威胁与控制缺口

| 威胁 | 现有控制 | 剩余暴露 |
|---|---|---|
| 未授权控制面访问 | session/API key、role、lockout、Gateway loopback、Host allowlist、严格安全头 | 目标防火墙/VPN/NAT/TLS/proxy/browser 仍未验证；远程访问无现行设计 |
| 凭证窃取 | git/docker ignore、Secure cookie、TLS generation | 当前模拟 HTTP、历史/日志/镜像未扫描 |
| 风险开关绕过 | 多层 kill checks、reduce-only exception | 最终 submit TOCTOU |
| 重复/幽灵订单 | clOrdId、command persistence、reconciliation | timeout/crash 全组合未故障注入 |
| 假回滚 | token、dual operator、audit status | profile rollback 不改 live payload |
| 假健康 | container health、heartbeat、metrics | 业务 task 可死、部署不查 trading-ready |
| 数据/参数投毒 | schema checks、quality gate、fingerprint、approval | runtime DDL、manual migrations、test selection bias |
| 回测收益夸大 | costs、Gold quality、OOS label | same-bar fill、test reuse、简化成交模型 |
| 资源耗尽 | limits、pool timeout、账户 lockout、登录有界 worker 与每进程三维限流 | DB pool 总量、跨进程集中限流、目标负载/慢连接无验收 |
| 供应链漂移 | pyproject lower bounds、container images | Phase 3T 已加入 Python lock/hash 和外部 image digest；APT、SBOM、CVE/license/secret/provenance、clean build 与远端 gate 仍缺失 |

## 核心不变量

### 订单与资金

1. 每个经济订单有稳定、唯一、可对账 identity。
2. 风险增加订单必须在最终 outbound 时持有当前、未撤销的 permission generation。
3. halt 后只允许可证明降低风险的动作；unknown intent 一律阻断。
4. venue 结果不明确时不得自动重复提交，先以只读订单/成交查询对账。
5. 订单、成交、现金、费用、funding、lot、position 和 ledger 可守恒闭环。

### 状态与恢复

6. PostgreSQL 资金事实优先于 Redis/NATS/UI；缓存必须带 scope/version/as_of。
7. 旧消息、重复消息、跨 scope 消息不能回退或污染新状态。
8. recovery/reconciliation/account/instrument/price 任一 unknown/stale 都阻断开仓。
9. critical task 不工作时 readiness 必须失败或系统显式 halt。
10. 部署成功必须证明应用版本、schema 版本、参数版本和运行 profile 一致。
11. INTEREST stream 的 publisher 只能在本部署代次的必需 durable consumers 就绪后启动；readiness 未知或存储异常必须失败关闭。

### 研究与治理

12. 决策时只能使用当时已可获得的数据；成交发生在信息可用之后。
13. test 不参与候选选择；所有调参次数和数据版本可追踪。
14. 回测、paper、live 使用同一策略/风控语义，允许差异必须显式版本化和量化。
15. apply/rollback 只有在 runtime 值读回后才能报告完成。
16. 文档、UI、HTTP 状态不得把 pending/degraded/unknown 表述成 succeeded/healthy。

## 威胁情景优先级

- 首要：`FS-001` 假回滚、`FS-002` halt 竞态、`FS-006/007` 假健康、`FS-003/004` 验证失真。
- 次要：Gateway 暴露与 auth DoS、数据库连接耗尽、schema 漂移、消息启动竞态。
- 持续治理：依赖锁定、辅助技术、文档/配置 drift、历史证据标记。

## 人工验证要求

任何 P1 修复都必须由至少一名未实施该修复的人复核证据；live profile 的回滚、kill switch、恢复和账户模式需要受控只读/模拟演练。禁止通过真实资金试单证明安全。
