# FS-009 Schema 单一迁移入口与启动失败关闭设计及实施范围

> 文档状态：Phase 3E 实施任务 / 设计冻结  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3D 变更  
> 核对范围：root migration ledger、RDP ORM baseline、Batch B runner、Gateway/RDP daemon 启动、Compose 与 deploy script  
> 运行时边界：只做静态和内存替身验证；未读取 `.env.*`，未连接或修改任何真实/本地 Postgres，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段收口 `FS-009` 的三个确定性路径：RDP Batch B 不在标准迁移入口、应用启动执行/吞掉 DDL、同一代码 revision 无法由 migration ledger 判断是否完整。目标是建立一个部署期显式 schema 作业：先应用主交易 root migrations，再创建 RDP ORM baseline 并按固定顺序应用全部 Batch B；应用进程在 managed profile 中只验证 migration contract，任何缺失、checksum 漂移或 RDP 校验异常均阻断启动。

本阶段不连接数据库，不生成“当前生产 schema 已一致”的结论，也不声称完成空库、历史克隆、部分失败库的 manifest 全等与 app+schema rollback 演练。那些是 FS-009 最终关闭和 G6 PASS 的必要运行证据。

## 2. 当前行为与根因

修复前隔离复现把 `aats.data_platform.db.run_migrations()` 替换为抛出 `RuntimeError`；Gateway 记录 warning 后仍进入 lifespan：

```text
rdp_schema_ensure_failed: RDP tables may not exist ...
{'rdp_schema_failure': 'RuntimeError', 'gateway_lifespan_entered': True}
```

静态链路同时证明：

- `run_migrations()` 只调用 `create_rdp_schema()`；
- `create_rdp_schema()` 使用 `RdpBase.metadata.create_all()` 和两个 bespoke ALTER helper；
- `BATCH_B_STAGES` 有 13 个包含 ALTER/VIEW/CHECK/precision 的 SQL stage，但只由手工脚本调用；
- Batch B runner 没有 revision/checksum ledger，同名 SQL 被修改后无法识别；
- 主交易 `build_storage_backends()` 在每个应用进程启动时 create/apply root schema；
- Gateway 的 RDP URL 在 base common env 中不统一，失败又被吞掉。

根因不是单个漏表，而是迁移所有权、时机、完整性和失败姿态没有统一 contract。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| root migration runner | 显式 apply 或只读 validate root migration 文件集合与 checksum |
| Batch B runner | 固定有序 stage、advisory lock、ledger/checksum、前置顺序与 rollback ledger 同步 |
| RDP schema service | 显式 apply = ORM baseline + 全 Batch B；validate = ORM table/column surface + ledger/checksum |
| schema job script | 唯一部署期命令，一次串行处理 profile DB 与 `aats_research` |
| application startup | managed profile 只验证，不 create/ALTER；验证失败不 ready |
| deploy | image build 后、app 启动前运行 one-shot schema job；失败立即停止流程 |

RDP ledger 行至少包含 `version`、`checksum`、`applied_at`。stage result 区分 applied 与 already-applied；checksum mismatch 失败关闭。

## 4. 输入/输出接口

新增/收紧接口：

- `apply_rdp_migrations(settings) -> BatchBReport`：显式 DDL；
- `validate_rdp_schema(settings) -> None`：只读验证；
- `validate_current_migrations(runtime) -> None`：只读 root migration 集验证；
- `scripts/apply_schema_migrations.py`：部署期唯一综合入口；成功 exit `0`，任一失败非零；
- `run_migrations()` 保留兼容别名但只允许显式初始化调用方使用，运行服务迁移到 validator。

不得在日志输出 DSN、用户名、密码或异常中可能含凭证的连接 URL。

## 5. 数据库 schema、表、索引与约束

新增 `governance.rdp_schema_migrations`：

```text
version varchar(256) primary key
checksum varchar(128) not null
applied_at timestamptz not null
```

该 bootstrap ledger 由迁移 runner 在 `governance` schema 已建立后创建。其余 schema 目标来自当前 `RdpBase.metadata` 与 `BATCH_B_STAGES`，本阶段不改业务表定义或既有 SQL 内容。

root `schema_migrations` 继续保存主交易 SQL 文件；RDP ledger 保存 RDP Batch B。综合作业是唯一执行入口，但两个数据库/迁移域保持不同 ledger，避免把不同 DSN 的状态伪装为一张表。

## 6. 事务、一致性与并发

PostgreSQL runner 使用固定 session advisory lock 串行化 RDP apply/rollback。runner 移除 SQL 文件的历史外层 `BEGIN/COMMIT`，由同一个 runner transaction 原子提交 DDL 与 ledger 行；外层 transaction 包装缺失/重复时失败关闭。checksum 与已记录值不同立即失败。

forward stage 必须满足此前所有 canonical stage 已记录；rollback 只能逆序移除当前已应用 suffix，防止回滚中间 stage 后保留依赖它的后续 schema。

## 7. 授权、认证与数据安全

不新增 HTTP 写接口。schema job 只在部署编排内部运行，复用容器已有数据库权限；不得打印 DSN 或 `.env.*`。本轮不会执行 job，也不会请求真实数据库凭证。Gateway 健康失败只呈现固定错误类型，不返回 SQL 或连接详情。

## 8. 错误处理与幂等

- migration 文件缺失、stage 非法、顺序缺口、ledger 缺失、checksum mismatch：失败关闭；
- 已记录且 checksum 相同：skip，不重跑；
- SQL 失败：记录 stage failed 并停止，后续 stage 不执行；
- Gateway/RDP daemon validator 失败：启动失败并清理，不能 warning 后 ready；
- deploy schema job 非零：不得进入 app up；
- 重复运行综合作业应全部 already-applied 且结果等价；
- validator 严禁 `CREATE TABLE IF NOT EXISTS` 等写入。

## 9. 状态转换与生命周期

```text
build image
  -> stop old app (FS-007 后续仍需一致回滚)
  -> infra ready
  -> explicit schema job
       -> root apply + validate
       -> RDP ORM baseline + Batch B ledgered apply + validate
       -> any failure: deploy nonzero, app remains stopped
  -> app processes start
       -> root validate-only (managed profile)
       -> RDP validate-only (Gateway/RDP daemons/jobs)
       -> mismatch: process startup fails
       -> valid: continue readiness
```

本阶段不把 schema job 成功等同于 trading-ready；它只是 FS-007 packet 的一个字段。

## 10. 缓存与性能

无缓存。validator 启动时读取固定数量 ledger 行、ORM table/column metadata，复杂度与 78 表/当前 migration 数线性相关；不扫描业务数据。advisory lock 只在显式 migration job/rollback 持有，不在每个请求路径使用。

## 11. 日志、监控与审计

schema job 记录 profile identity（不含 DSN）、root applied count、RDP applied/skipped stage names 和最终 revision。失败只记录 stage/error type；详细 DB error 保留在受控日志，不进入 HTTP response。deploy 报告必须包含 schema job 已通过事实，不能从“容器 healthy”反推。

## 12. 测试策略

新增纯内存/fake-engine 与静态对抗测试覆盖：

1. Gateway RDP validator 抛错时 lifespan 不 yield，且已启动资源被清理；
2. Batch B 已记录同 checksum 时 skip；
3. checksum mismatch 失败；
4. stage 失败停止后续且不写失败 stage ledger；
5. 缺前置 stage 的 partial forward 被拒绝；
6. 非 suffix rollback 被拒绝；
7. rollback 成功删除对应 ledger；
8. root validator 对缺 migration、checksum mismatch、unknown extra version 失败；
9. managed live profile 禁止 runtime auto create；
10. deploy 在 app up 前运行 schema job，且 job failure 由 `set -e` 阻断；
11. runtime service callers 不再调用 apply alias；
12. RDP common env 对所有 app 使用同一 `aats_research` URL。

随后运行 focused、数据库/迁移/部署相关单测、Ruff 与全量 unit。PostgreSQL integration、克隆库和 rollback 演练未获本轮环境授权，不伪装执行。

## 13. 迁移、回滚与兼容

首次使用新版 runner 时，既有手工应用 Batch B 的库没有 ledger；显式 schema job 会按 canonical 顺序重跑幂等 stage 并建立 ledger。不得直接人工插入 ledger“认领”未知 schema。

应用代码回滚到旧版本前必须确认旧版本是否理解新增 ledger/后续 schema；本阶段不自动回滚业务 schema。Batch B rollback 工具会同步 ledger，但生产使用仍需克隆演练和人工批准。没有 schema/app 一致回滚证据前 FS-007/009 均保持 OPEN。

## 14. 配置与环境隔离

managed `spot/derivatives/spot_live/derivatives_live` 固定 `database_auto_create_schema=false`；显式 migration job 不受该 runtime 保护位阻止。普通 memory/dev 非 managed 测试保持兼容。

Compose common env 为所有相关进程提供同一 `RDP_DATABASE_URL=.../aats_research`。不得从默认 localhost placeholder 连接或在失败后继续。

## 15. 代码组织与依赖

预计修改：

- `aats/storage/session.py`：root validate-only；
- `aats/data_platform/migrations/_batch_b.py`：ledger/checksum/order/lock；
- `aats/data_platform/db.py`：apply/validate 分离；
- Gateway、RDP daemon 与一批 research job caller：validate-only；
- managed profiles、Compose common env、deploy schema step；
- `scripts/apply_schema_migrations.py` 与 `rdp_init_db.py`；
- FS-009 单元/静态测试和当前审计文档。

不新增第三方依赖，不修改交易/API/OrderState 语义。

## 16. 文档、运维手册与验收标准

本阶段验收标准：

- 修复前 Gateway migration failure 继续 ready 的复现变为 lifespan 失败；
- 标准 `run_migrations/apply_rdp_migrations` 覆盖全部 canonical Batch B 且有 checksum ledger；
- managed app startup 不执行 create_all/ALTER，只验证；
- deploy 在 app up 前显式执行综合 schema job，失败不继续；
- 无凭证/DSN 输出；
- focused、相关、全量 unit 与 Ruff 通过；
- 审计状态至多更新为 `PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN`；
- 真实资金上线继续 NO-GO。

最终关闭仍需在空库、当前生产克隆、缺 stage 库和部分失败库上执行 forward/retry/rollback，导出 schema/table/column/type/nullability/default/index/constraint/view/function manifest 并证明完全一致；还需验证旧 app/image 与 schema 的兼容回滚。静态代码和 mock tests 不能代替这些证据。
