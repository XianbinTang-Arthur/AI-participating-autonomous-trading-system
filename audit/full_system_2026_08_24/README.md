# AATS 全系统深度审计（2026-08-24）

本目录保存对代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56` 的审计证据。`00`–`20` 是 Phase 1/2 只读审计快照；Phase 3A 之后的修复证据从 `21` 开始追加，不回写历史复现结论。当前整改候选仍以同一 HEAD 为起点，Phase 3A–3W 已追加 FS 专项修复及一次全量变更复审；阅读者必须同时查看 Git status 和对应 remediation 记录。

## 结论边界

- 当前结论是“已发现阻断上线的问题”，不是上线批准。
- `VERIFIED` 表示可由当前代码、配置、测试或本次只读运行检查直接证明。
- `INFERRED` 表示证据链完整但缺少受控故障注入或真实目标环境复现。
- `UNKNOWN` 表示当前证据不足；不得把 UNKNOWN 写成正常或安全。
- 容器 `healthy`、HTTP 200、静态配置解析和单元测试通过均不等于 trading-ready。
- 本次未读取或展示任何 `.env.*` 凭证，未查询余额、持仓、订单或策略收益，未提交订单，未写生产数据库。

## 阅读顺序

1. [00-executive-summary.md](00-executive-summary.md)
2. [21-fs-002-remediation.md](21-fs-002-remediation.md)
3. [22-fs-001-profile-rollback-fail-closed.md](22-fs-001-profile-rollback-fail-closed.md)
4. [23-fs-003-backtest-causal-timing-remediation.md](23-fs-003-backtest-causal-timing-remediation.md)
5. [24-fs-006-critical-task-supervision-remediation.md](24-fs-006-critical-task-supervision-remediation.md)
6. [25-fs-009-schema-single-truth-remediation.md](25-fs-009-schema-single-truth-remediation.md)
7. [26-fs-007-deployment-fail-closed-remediation.md](26-fs-007-deployment-fail-closed-remediation.md)
8. [27-fs-005-gateway-loopback-containment.md](27-fs-005-gateway-loopback-containment.md)
9. [28-fs-020-browser-security-headers-remediation.md](28-fs-020-browser-security-headers-remediation.md)
10. [29-fs-019-operator-login-async-isolation.md](29-fs-019-operator-login-async-isolation.md)
11. [30-fs-016-nats-peer-readiness-remediation.md](30-fs-016-nats-peer-readiness-remediation.md)
12. [31-fs-006-critical-task-progress-watchdog.md](31-fs-006-critical-task-progress-watchdog.md)
13. [32-fs-002-short-lived-trading-permission-lease.md](32-fs-002-short-lived-trading-permission-lease.md)
14. [33-fs-001-profile-apply-fail-closed.md](33-fs-001-profile-apply-fail-closed.md)
15. [34-fs-014-ohlcv-fill-realism-containment.md](34-fs-014-ohlcv-fill-realism-containment.md)
16. [35-fs-017-fs-018-dashboard-accessibility.md](35-fs-017-fs-018-dashboard-accessibility.md)
17. [36-fs-010-managed-profile-unknown-key-fail-closed.md](36-fs-010-managed-profile-unknown-key-fail-closed.md)
18. [37-fs-011-legacy-run-local-fail-closed.md](37-fs-011-legacy-run-local-fail-closed.md)
19. [38-fs-015-replay-short-bias-parity.md](38-fs-015-replay-short-bias-parity.md)
20. [39-fs-021-ci-quality-gate.md](39-fs-021-ci-quality-gate.md)
21. [40-fs-022-reproducible-dependencies.md](40-fs-022-reproducible-dependencies.md)
22. [41-fs-008-database-connection-budget.md](41-fs-008-database-connection-budget.md)
23. [42-fs-004-research-selection-holdout.md](42-fs-004-research-selection-holdout.md)
24. [43-phase3w-post-audit-full-change-review.md](43-phase3w-post-audit-full-change-review.md)
25. [17-p1-adversarial-verification.md](17-p1-adversarial-verification.md)
26. [18-p1-verification-matrix.md](18-p1-verification-matrix.md)
27. [19-p0-hunt.md](19-p0-hunt.md)
28. [20-go-no-go-gates.md](20-go-no-go-gates.md)
29. [15-consolidated-risk-register.md](15-consolidated-risk-register.md)
30. [16-remediation-roadmap.md](16-remediation-roadmap.md)
31. 按专题阅读 `01` 至 `13`
32. 用 [14-coverage-matrix.md](14-coverage-matrix.md) 和 `review-coverage.csv` 判断结论覆盖范围
33. 用 [AUDIT_STATE.md](AUDIT_STATE.md) 续审或处理基线漂移

## 报告清单

| 文件 | 主题 |
|---|---|
| 00 | 执行摘要与上线判断 |
| 01 | 系统地图、入口与真实运行链路 |
| 02 | 代码正确性 |
| 03 | 量化、行情、研究与回测 |
| 04 | 执行、风控与资金安全 |
| 05 | 架构与跨进程一致性 |
| 06 | 前后端契约 |
| 07 | UI、UX 与可访问性 |
| 08 | 数据库、事务与状态真相源 |
| 09 | 安全与凭证边界 |
| 10 | 可靠性、恢复与可观测性 |
| 11 | 测试策略与覆盖 |
| 12 | 性能、扩展性、依赖与构建复现 |
| 13 | 资金安全威胁模型与核心不变量 |
| 14 | 覆盖矩阵与未审边界 |
| 15 | 合并风险登记簿 |
| 16 | 分阶段修复路线图（仅建议，未实施） |
| 17 | 九项 P1 的对抗性全路径复核与最终裁定 |
| 18 | P1 严重度、资本风险与运行验证矩阵 |
| 19 | P1 邻近组合的 P0 狩猎与未知边界 |
| 20 | 真实资金上线硬门禁、条件门禁与 readiness packet |
| 21 | FS-002 Kill Switch P0 修复、验证、残余风险与当前 OPEN 状态 |
| 22 | FS-001 profile rollback 虚假成功收口、真 reverse saga 缺口与当前 OPEN 状态 |
| 23 | FS-003 回测因果时间契约修复、旧证据失效、重跑/独立复核 OPEN 状态 |
| 24 | FS-006 关键 task-exit 监督、health 失败路径、hang/lag 与运行验证 OPEN 状态 |
| 25 | FS-009 显式 schema job、root/RDP ledger、启动失败关闭和克隆 manifest/rollback OPEN 状态 |
| 26 | FS-007 实盘入口硬隔离、模拟部署失败关闭/evidence 与 readiness/一致回滚 OPEN 状态 |
| 27 | FS-005 Gateway loopback、本地入口失败关闭与目标网络验证 OPEN 状态 |
| 28 | FS-020 Host 失败关闭、浏览器安全头与目标 TLS-browser 验证 OPEN 状态 |
| 29 | FS-019 Operator 登录异步隔离、有界 worker、每进程限流及分布式/负载验证 OPEN 状态 |
| 30 | FS-016 NATS/hybrid peer readiness 失败关闭、部署代次隔离及目标 startup/restart 验证 OPEN 状态 |
| 31 | FS-006 固定周期任务成功进度 deadline、stalled 失败路径与事件驱动/目标运行验证 OPEN 状态 |
| 32 | FS-002 generation-scoped 15 秒交易许可、全分区 TTL 收敛与目标分区验证 OPEN 状态 |
| 33 | FS-001 profile apply 错误成功收口、apply/rollback 双失败关闭与真实 runtime activation OPEN 状态 |
| 34 | FS-014 OHLCV participation-cap、partial fill、成本分解与 L2 校准 OPEN 状态 |
| 35 | FS-017/018 Dashboard 原生 modal/focus/reduced-motion 收口与目标辅助技术验证 OPEN 状态 |
| 36 | FS-010 managed 伪配置删除、unknown-key 失败关闭、生成文档防回退与目标启动验证 OPEN 状态 |
| 37 | FS-011 legacy run-local 无配置副作用失败关闭与外部调用方迁移 OPEN 状态 |
| 38 | FS-015 replay/production short-bias gate 对齐与历史证据重跑/独立复核 OPEN 状态 |
| 39 | FS-021 基础 CI/warning gate、Long/Short mock 修复与远端 enforcement/integration OPEN 状态 |
| 40 | FS-022 Python hashed lock、外部 image digest 与 APT/SBOM/scan/clean-build OPEN 状态 |
| 41 | FS-008 角色化连接池、声明 topology ceiling=150、名义余量 47 与目标负载/瞬时路径 OPEN 状态 |
| 42 | FS-004 train/valid 双门、test 内容封存与最终 OOS/历史 lineage 审计 OPEN 状态 |
| 43 | Phase 3W 全量变更复审、新发现收口、严格全量回归及目标运行验证 OPEN 状态 |

## 续审纪律

续审前必须重新核对 HEAD、branch、tracked/staged diff 与未跟踪文件。若 HEAD 或已跟踪内容变化，所有受影响文件降级为 `PARTIALLY REVIEWED`；未经复核不得沿用本目录的“已验证”结论。
