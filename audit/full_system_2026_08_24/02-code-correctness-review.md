# 02 代码正确性审查

> 状态边界：本文件主体保存 Phase 1/2 修复前 finding。Phase 3B/3M 已将 FS-001
> profile rollback/apply 都改为授权后无写入 `501`；真实 runtime activation/readback
> 仍 OPEN。当前证据见 `22`、`33`，上线门禁见 `20`。

## FS-001 — 回滚接口报告成功但没有回滚运行参数

- 严重度：P1；置信度：高；类别：correctness / operator safety
- 状态：VERIFIED
- 位置：`aats/api/rdp_profile_routes.py:542-571`
- 证据：函数验证 token、actor 与双人审批后，只执行 `UPDATE governance.recommendations SET status='rolled_back'`。注释明确写明真实反向 saga 尚未实现；响应仍返回 `ok: true` 和 `status: rolled_back`，仅附带 `pending_live_rollback: true`。
- 触发：Operator 调用 profile recommendation rollback。
- 后果：治理记录与实际 live payload 分叉；人和自动化可能误以为风险参数已恢复，继续交易。
- 复核：检索调用方、UI 与测试；没有找到该 profile endpoint 对运行参数逆向写回的实现或测试。其他 parameter rollback 流程的真实实现不能替代本 endpoint。
- 建议：在真实逆向 saga 完成前返回 409/501 或明确 `ok:false`；完成后以单事务/可恢复 saga 写 live payload、apply history、active set、审计记录，并用读回校验后才返回成功。

## FS-010 — managed profile 配置键被静默忽略

- 严重度：P3；置信度：高；类别：configuration correctness
- 状态：VERIFIED
- 位置：`configs/strategy_profiles/{spot,spot_live,derivatives,derivatives_live}.yaml` 的 `strategy_profile_auto_rollback_enabled`；`aats/bootstrap/settings.py:140`
- 证据：四个 profile 和现行配置文档都声明该键；`AATSSettings` 没有对应字段，并设置 `extra="ignore"`。受控键集合比较仅发现这一项未知 profile 键。
- 触发：Operator 试图用该键禁用/启用自动回滚。
- 后果：配置看似生效，实际无效；当前值恰好为 true 不能证明未来变更安全。
- 建议：要么加入受测试的 settings 字段并接入行为，要么删除键和文档；CI 对 managed YAML 使用 `extra="forbid"` 式离线校验。

### Phase 3P 当前状态补充

上列证据冻结 Phase 1/2 修复前事实。Phase 3P 确认该键没有 Settings 字段或行为
消费者，已从四个 managed YAML、生成器和现行字段参考删除；没有通过增加空壳字段
伪装自动回滚能力。managed loader 现在要求 strategy YAML 为 mapping，并对 runtime
defaults 与 YAML 全部 key 使用 `AATSSettings.model_fields` 失败关闭校验；未知 key 不能
进入 `load_settings()`。8 项 focused、56 项相关和 4345 项全量 unit 通过。

目标 profile 启动、仓库外 overlay 盘点、生成器 clean-run 与独立复核仍未完成，因此
状态为 `CODE REMEDIATED / MANAGED UNKNOWN-KEY FAIL-CLOSED / TARGET STARTUP VERIFICATION OPEN`；
详见 [`36`](36-fs-010-managed-profile-unknown-key-fail-closed.md)。

## FS-011 — legacy 本地入口与当前函数签名不兼容

- 严重度：P3；置信度：高；类别：dead/stale code
- 状态：VERIFIED
- 位置：`scripts/run_local.py` 与 `apps/decision_engine/main.py`
- 证据：legacy runner 把 decision main 当作可 await 且接受 kwargs 的函数；当前 main 是无参同步退出码入口。现行文档已经把它标为 legacy/unavailable。
- 触发：开发者按旧习惯运行本地 paper loop。
- 后果：启动即失败，并可能误导测试路径选择。
- 建议：删除/归档入口，或让它显式打印迁移指引并非零退出；不要恢复一条未经当前架构验证的旁路。

### Phase 3Q 当前状态补充

上列证据冻结 Phase 1/2 修复前事实。Phase 3Q 保留旧路径和参数识别，但移除 asyncio、
dotenv loader 与 decision runtime 调用；脚本现在只输出迁移指引并返回 `2`。6 项 focused、
51 项相关和 4351 项全量 unit 通过。committed candidate 独立复核与仓库外调用方迁移
仍 OPEN，状态为 `CODE REMEDIATED / LEGACY ENTRY FAIL-CLOSED / EXTERNAL CALLER
MIGRATION OPEN`；详见 [`37`](37-fs-011-legacy-run-local-fail-closed.md)。

## FS-012 — Gateway 对 RDP schema ensure 失败仅告警继续

- 严重度：P2；置信度：高；类别：startup correctness
- 状态：VERIFIED
- 位置：`apps/api_gateway/main.py:120-127`
- 证据：`run_migrations()` 的任意异常被广泛捕获并只写 warning；Gateway 继续 ready。
- 触发：RDP 数据库不可达、DDL 权限不足、schema 漂移或迁移冲突。
- 后果：容器与 `/healthz` 成功，但 AI Config/RDP API 部分失效；部署健康检查仍可能通过。
- 建议：把 RDP 可用性纳入独立 readiness/degraded contract；live 上线 gate 对必需组件 fail-closed，非必需组件必须在 UI/告警明确降级。

## 交叉核对结论

- 关键持久化路径普遍使用显式事务、rollback、唯一键/ON CONFLICT 和 outbox；没有发现 fee 符号在已审路径中的直接反转。
- `OrderState` 的 PostgreSQL、payload、Redis 三层一致性属于高风险面，已抽样检查主要写路径，但未逐文件封板。
- 大量 `except Exception` 在循环中用于保持服务运行；部分会记录 structured health，部分只记录 warning。需要按“可恢复业务错误”和“任务应退出的结构错误”分类，而不是统一吞掉。
- Ruff 对应用目录没有输出错误；9 个错误均位于 tests，但仍表示仓库级 lint gate 不是绿色。
