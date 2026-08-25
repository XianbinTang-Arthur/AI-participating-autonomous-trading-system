# 25 FS-009 Schema 单一迁移入口与启动失败关闭整改证据

> 阶段：Phase 3E  
> 日期：2026-08-24  
> 起始 HEAD：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 分支：`codex/fs-002-kill-switch-p0`  
> 工作区：包含尚未提交的 Phase 3A–3E 变更  
> 当前裁定：`PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 整改范围与证据边界

本阶段收口 `FS-009` 的确定性代码路径：将主交易 root migrations、RDP ORM baseline 和 13 个 Batch B stage 收口到部署期显式一次性 job；为 RDP 增加 version/checksum ledger、有序前置、并发锁和 rollback suffix contract；managed 应用启动改为只读校验，且 Gateway 在任何 readiness 或后台 task 之前失败关闭。

本阶段没有读取 `.env.*`，没有连接或修改任何本地/克隆/live Postgres，没有运行 Docker/Compose，没有部署或访问交易所。PostgreSQL integration 用例已加入但因显式环境门未开启而跳过。因此，不得从本记录推导当前生产 schema 已等价。

实施前设计和 16 节 SOW 见 [`docs/task/fs_009_schema_single_truth_sow_2026_08_24.md`](../../docs/task/fs_009_schema_single_truth_sow_2026_08_24.md)。

## 2. 修复前确定性证据

对 Gateway lifespan 中的 `run_migrations()` 注入 `RuntimeError`，原实现记录 warning 后仍进入 lifespan：

```text
rdp_schema_ensure_failed...
{'rdp_schema_failure': 'RuntimeError', 'gateway_lifespan_entered': True}
```

静态调用链同时证明：

- RDP `run_migrations()` 只执行 ORM `create_all()` 和少量 bespoke ALTER helper；
- Batch B 包含 ALTER/VIEW/CHECK/精度变更，却只能由手工 stage 工具触发；
- Batch B 没有 revision/checksum ledger，同一 Git revision 无法识别是否完整执行；
- managed 主交易进程在启动时可以自行 create/apply root schema，多进程共享 DDL 所有权；
- Gateway 的 RDP 错误语义与 RDP daemon 不同，存在部分 ready；
- deploy 没有 app up 前的独立 schema job。

## 3. 已实施变更

### 3.1 主交易 root migration 模式

`aats/storage/session.py` 新增 `validate_current_migrations()`，仅对 PostgreSQL 只读比较当前 `migrations/*.sql` 的完整文件集与 `schema_migrations` ledger。`missing`、`unknown`、`mismatched checksum` 均拒绝启动。

`build_storage_backends()` 现在明确区分：

- 显式非 managed 初始化模式：`create_schema()` + `apply_current_migrations()`；
- managed profile：`database_auto_create_schema=false`，只执行 ledger 和运行 schema 校验。

四个 managed profile 全部固定禁止 runtime schema mutation。

### 3.2 RDP Batch B ledger 与事务

`aats/data_platform/migrations/_batch_b.py` 现在：

- 使用 `governance.rdp_schema_migrations(version, checksum, applied_at)`；
- 对前向与 rollback 使用 PostgreSQL session advisory lock 串行化；
- 按 `BATCH_B_STAGES` 的 13 个 canonical stage 校验全部前置；
- 已记录且 checksum 相同时 skip，checksum 变化立即失败；
- SQL 文件必须恰好有一层 legacy `BEGIN/COMMIT`；runner 移除它们，使 DDL 与 ledger insert/delete 在同一 transaction 提交；
- 前向失败立即停止后续 stage，失败 stage 不记账；
- rollback 只能删除当前已应用 suffix，不允许中间抽除前置。

### 3.3 RDP apply/validate 分离

`aats/data_platform/db.py` 将写入和运行验证分离：

- `apply_rdp_migrations()`：显式 ORM baseline + 全 Batch B + 最终校验；
- `validate_rdp_schema()`：只读检查 7 个 schema、78 张 ORM table/column surface 和完整 Batch B ledger/checksum；
- `run_migrations()` 仅保留为显式初始化兼容别名，运行调用方已全部改用 validator。

RDP daemon 在领取/恢复任务前校验。历史保留的 `--ensure-schema` CLI 参数现在明确标注为 validate-only，不再在 ingest/replay/research job 内隐式执行 DDL。

### 3.4 Gateway 启动失败关闭

Gateway lifespan 在 `build_runtime()`、readiness announcement/peer barrier、runtime background tasks 和 dashboard snapshot plane 之前执行 RDP 只读校验。任一错误直接阻止 lifespan，不产生业务后台副作用。

修复中一度将 validator 放在 background tasks 之后；兼容性复核发现这仍有短窗口后已前移，对抗测试断言 runtime 未启动且 dashboard 未启动。

### 3.5 部署期综合作业

`scripts/apply_schema_migrations.py` 串行执行：

```text
root create/apply -> root ledger/runtime validate
  -> RDP ORM baseline -> full Batch B -> RDP validate
```

成功只输出迁移文件/stage 名称与状态，不输出 DSN。`deploy.sh` 现在先 build 新镜像，再 down 旧栈，启动基础设施后以覆盖命令的 `aats-gateway` one-shot container 运行 schema job，之后才 app up。Schema job 非零由 shell `set -e` 阻断。

复核曾发现初版实施错用 `aats-rdp-daemon` 容器；该 service 注入的 `AATS_PROCESS_ROLE=rdp-daemon` 不在 `AATSSettings` 合法 role 集，会使一次性 job 在读 settings 时失败。已改为复用合法 gateway/monolith role 的容器环境；命令覆盖确保它不启动 API 或交易路径。

## 4. 隔离对抗证据

新增 16 项 FS-009 单元/静态对抗测试：

1. 完整 Batch B 首次 apply 全记账，第二次全 skip；
2. DDL 与 ledger row 使用同一 transaction；
3. SQL 缺少外层 transaction wrapper 失败；
4. 已记账 checksum mismatch 失败；
5. stage 失败停止后续且不记失败 stage；
6. partial forward 缺前置失败；
7. 非 suffix rollback 失败，suffix rollback 同步 ledger；
8. root missing/mismatched/unknown ledger 都失败，完全一致通过；
9. Gateway RDP validator 失败时不调用 `build_runtime`、不启动 runtime/dashboard；
10. 四个 managed profile 全部禁止 runtime DDL；
11. deploy 先 build 后 down、schema job 在 app up 前，且不复用非法 rdp-daemon role；
12. runtime RDP callers 只使用 validator；
13. Compose common env 统一 RDP 数据库身份。

另增 1 项受环境开关保护的 PostgreSQL integration 用例，用于真 Postgres 的 full apply/idempotent/rollback-repair/validate contract；本阶段只完成 collection，未执行容器。两个原有单 stage SQL integration 测试已改为直接测试孤立 SQL contract，不通过伪造前置 ledger 绕过生产 runner。

## 5. 测试与静态验证

| 检查 | 结果 |
|---|---|
| FS-009 focused | `16 passed` |
| schema/gateway/deploy/RDP 相关回归 | `152 passed, 3 skipped, 2 subtests passed` |
| managed profile/bootstrap/database 回归 | `68 passed, 6 subtests passed` |
| 最终全量 unit | `4186 passed, 30 skipped, 1666 warnings, 85 subtests passed in 85.02s` |
| PostgreSQL narrow integration | `7 collected`；未开启 `AATS_RUN_POSTGRES_INTEGRATION`，`7 skipped` |
| Ruff `aats/ --fix` | `All checks passed` |
| Ruff 变更的 apps/scripts/tests | `All checks passed` |
| deploy shell 语法 | `bash -n scripts/deploy.sh` 通过 |

首次扩大相关回归出现 3 个 `tmp_path` setup error，原因是 Windows 默认 pytest 临时目录权限拒绝；改用 audit 内 `--basetemp` 后同组 `152 passed, 3 skipped`。这不是代码断言失败，两次结果均未被隐藏。

一次工具调用误将 `scripts/deploy.sh` 作为 Python 文件传给 Ruff，产生 shell-as-Python syntax noise；随后已用正确文件集重跑 Ruff，并独立用 `bash -n` 验证 shell。该误用不计为代码失败或通过。

Warnings 与既有审计一致：主要是 sqlite datetime deprecation、LongShort poller AsyncMock contract，另加 pytest cache 目录权限 warning。本阶段没有把 warning 数量写成零。

## 6. 已验证、未验证与未知

### 静态/隔离已验证

- 标准显式 RDP apply 调用完整 Batch B chain，不再只有 ORM `create_all`；
- root 和 RDP 都对当前 checkout 执行 exact ledger/checksum 校验；
- RDP stage DDL 与 ledger 事务原子性有机器断言；
- 前置、幂等、checksum drift、失败停链和 rollback suffix 语义有隔离覆盖；
- managed 应用启动只读校验，Gateway 不再吞错 ready；
- deploy 在 app up 前运行 one-shot job，非法 process role 已纠正；
- 兼容 `--ensure-schema` 不再声称自己执行 migration；
- 不在成功输出或当前文档示例中显示 DSN/密码。

### 运行时未验证

- 真 Postgres 16 上 78 表 + 13 stage 的首次 apply 与重试；
- 当前实际 WSL2 Compose profile 的 one-shot container 环境、权限与非零传播；
- 空库、当前生产克隆、缺 stage 库和部分失败库的 forward/retry；
- 完整 schema/table/column/type/nullability/default/precision/index/constraint/view/function manifest 全等；
- 迁移期间无旧应用的可用性窗口、锁时间和 RTO。

### 仍未实现/未知

- 可验证的 app image + schema + parameter 一致回滚；
- Batch A 历史 hardening 与 Batch B/root 的最终单一 manifest 治理方案；
- 下一 revision 的 forward-only 兼容政策与老 app 回滚窗口；
- schema job 成功如何纳入 FS-007 不可变 trading-readiness packet。

## 7. 当前裁定与上线影响

原 finding 的“同 revision 可由 create_all 与手工 Batch B 形成不同 schema，Gateway 又可吞错 ready”代码路径已大幅收紧。但 `FS-009` 的最终验收要求真 Postgres 的 fresh/upgrade/partial failure/rollback 与完整 manifest 证据，这些本阶段均未运行。当前状态因此是：

```text
PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN
```

G6 从原始 `FAIL` 更新为 `PARTIAL / 未放行`。关闭前至少仍需：

1. 在隔离 Testcontainers/生产克隆上执行新 PostgreSQL integration 与综合 job；
2. 对四类初始 schema 输出完整 manifest 并证明全等；
3. 注入 SQL 失败、ledger 写失败、锁竞争、app up 失败与 rollback 失败；
4. 在克隆环境验证新 app/schema 失败后的旧 image + schema + parameter 一致恢复；
5. 由独立 reviewer 核对迁移集、ORM/bespoke helper 与回滚数据破坏风险。

Phase 3E 不构成部署或真实资金授权。**REAL-MONEY PRODUCTION: NO-GO**。
