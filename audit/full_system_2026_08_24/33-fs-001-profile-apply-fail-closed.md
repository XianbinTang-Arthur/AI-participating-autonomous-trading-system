# 33 FS-001 Profile Apply 错误成功失败关闭收口记录

> 日期：2026-08-24  
> 阶段：Phase 3M  
> 起始代码基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3M 变更  
> 验证边界：静态代码、fake governance session/Request/Principal、Windows 单元测试  
> 未执行：真实 research/live DB、Redis/NATS、worker 热加载/读回、WSL2/Docker、部署、凭证读取、账户/交易所操作  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 本阶段关闭的错误成功路径

Phase 3B 已阻止 profile rollback 只改 recommendation 状态却返回成功，但相邻的
`POST /rdp/profile-recommendations/{rec_id}/apply` 仍会执行历史四步 Saga，并在四个
SQL step 返回完成后把 recommendation 标为 `applied`、返回 `ok=true`。

这不是可接受的运行时生效证明。该接口现已改为：

```text
valid auth/token + released recommendation + dual operator
  -> HTTP 501 profile_apply_not_implemented
  -> recommendation remains released
  -> no saga/live session/research write/live write
```

Phase 3M 只关闭错误成功语义，不声称已实现真实 profile activation。

## 2. 代码证据与根因

当前代码链的四项不一致可直接证明旧成功终态不真实：

1. `profile_apply_saga.step3_update_live_payload()` 用
   `payload->>'profile_id'` 查找 live activation；现行
   `StrategyProfileActivationState` 和 Postgres repository 使用的是
   `active_profile_id`；
2. Saga 写入三个 threshold key，但它们都不是 activation schema 字段；repository
   通过 `StrategyProfileActivationState.model_validate(row.payload)` 重建领域状态，
   不会把这些额外 key 变成有效 activation 字段；
3. `active_parameter_sets(scope='profile')` 的 `family/timeframe` 按 migration 和 Saga
   均为 `NULL`；runtime loader 却把所有行按 `family_timeframe` 建键，没有按
   `scope/profile_id` 分发到目标 worker 的协议；
4. 旧流程没有单调 generation、目标 worker、内存态 digest、ack 或 readback。

因此，旧流程最多证明某些数据库写入完成，不能证明 decision/execution runtime 已采用
目标参数。继续返回 `ok=true/applied` 会让 Operator、UI 或自动化产生错误终态认知。

## 3. 修复决策

- 保留 `require_write_access`、profile v2 apply token、session/token actor 一致性；
- 保留 recommendation 必须为 `released` 的状态门禁；
- 保留 approver/applier 分离；
- 安全检查通过后固定返回 `501 profile_apply_not_implemented`；
- 不检查/打开 live pool，不加载 parameter set，不计算 patch；
- 不创建或续跑 `apply_saga_operations`，不调用历史 Saga；
- 不更新 active set/history/live activation/recommendation；
- warning 日志只记录 recommendation id、当前状态和 actor，不记录 token 或参数值。

完整设计与后续真实协议验收条件见
`docs/task/fs_001_profile_apply_fail_closed_sow_2026_08_24.md`。

## 4. 变更文件

| 文件 | 变更 |
|---|---|
| `aats/api/rdp_profile_routes.py` | profile apply 改为授权、状态、双签之后零写入 `501`；移除 route 对 live session/Saga 的调用 |
| `tests/unit/test_fs001_profile_apply_fail_closed.py` | 覆盖 OpenAPI、零写入、无 Saga/live 调用、状态/双签优先和重复请求幂等 |
| `docs/task/fs_001_profile_apply_fail_closed_sow_2026_08_24.md` | Phase 3M 16 节设计、实施边界和关闭条件 |
| 现行 README/架构/运维文档 | 区分 combo 参数流程与已禁用的 profile apply/rollback |
| 本审计包 | 更新 FS-001、G2、NO-GO 和残余风险 |

历史 `profile_apply_saga.py` 与其内部单元测试保留作追溯证据，但生产 route 不再调用。
本阶段无 schema、配置、凭证或依赖变更。

## 5. 验证证据

### 5.1 直接 FS-001 与历史 Saga 回归

```text
.venv\Scripts\python.exe -m pytest \
  tests/unit/test_fs001_profile_apply_fail_closed.py \
  tests/unit/test_fs001_profile_rollback_fail_closed.py \
  tests/unit/test_profile_apply_saga.py -q

17 passed, 1 warning in 1.18s
```

其中新增 apply 路由测试为 5 项。它们把旧 parameter loader、patch、Saga operation、
Saga executor 与 live session 全部设为“若调用即失败”，验证 endpoint 没有进入这些路径。

### 5.2 RDP/Profile 扩大相关回归

使用仓库内全新 basetemp 重跑所有文件名含 `rdp` 或 `profile` 的 unit：

```text
321 passed, 1 skipped, 100 warnings, 6 subtests passed in 12.17s
```

首次原样运行同一集合时有 `251 passed, 1 skipped`，另 70 项统一在 `tmp_path`
fixture 创建阶段因 Windows 系统临时目录 `PermissionError` 中止；没有业务断言失败。

### 5.3 全量单元测试

仓库要求的原样命令：

```text
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
87 passed, 2 warnings, 1 setup error
```

setup error 是 Windows 系统临时目录
`C:\Users\<user>\AppData\Local\Temp\pytest-of-<user>` 的 `PermissionError`。随后使用
事前确认不存在的仓库内 basetemp 完整运行：

```text
4317 passed, 30 skipped, 1665 warnings, 85 subtests passed in 115.56s
```

警告仍主要是 SQLite datetime adapter deprecation 和既有 LongShort AsyncMock
`raise_for_status` 未 await；本阶段没有把 warning 当作通过证据之外的安全结论。

### 5.4 Ruff

定向检查：

```text
.venv\Scripts\python.exe -m ruff check \
  aats/api/rdp_profile_routes.py \
  tests/unit/test_fs001_profile_apply_fail_closed.py
All checks passed!
```

最终仓库级检查：

```text
.venv\Scripts\python.exe -m ruff check aats/ --fix
All checks passed!
```

变更 Markdown 本地链接检查覆盖 76 个文件、337 个本地链接，`broken=0`；
`git diff --check` 退出码为 0，仅输出工作区既有的 LF/CRLF 转换提示。路由独立导入并
读取 OpenAPI route status 得到 `501`。

## 6. 修复后接口契约

| 场景 | 当前结果 | 写入 |
|---|---|---:|
| token 缺失/无效 | 既有 403 | 0 |
| token actor 与 session 不一致 | 既有 403 | 0 |
| recommendation 不存在 | 既有 404 | 0 |
| recommendation 非 released | 既有 409 | 0 |
| approver 与 applier 相同/缺失 | 既有 409 | 0 |
| 所有安全校验通过 | 501 `profile_apply_not_implemented` | 0 |
| 重复合法请求 | 每次相同 501 | 0 |

返回体不包含 `ok`、`operation_id`、`steps_completed`、参数值或任何 `applied` 终态。
live pool 未配置也不再优先返回 503，因为根本阻塞条件是缺少安全的 runtime
activation/readback 协议，而不是连接池是否存在。

## 7. 剩余风险与真正关闭条件

Phase 3M 之后 profile recommendation 的 approve/release 仍可用于研究治理，但 apply 与
rollback 都不可用于改变运行参数。真实资金上线前仍需：

1. 定义 execution-owned profile 参数领域模型及 profile revision/parameter set 关系；
2. 用 migration 持久化 operation、不可变 from/to digest、单调 generation、目标 worker
   和 ack/readback；
3. 同一 profile 的 CAS/lock，阻止并发 apply/rollback 覆盖；
4. 由目标 worker 原子加载，并回报实际内存态 generation/digest；
5. research active set、live authority、history、UI/API 与 runtime readback 同代次一致；
6. 部分失败保持 pending/failed，可重放且不能出现成功终态；
7. 对称的 reverse Saga、crash/restart/timeout/消息丢失故障注入；
8. 对历史 `applied` recommendation/Saga 行做隔离对账，不能假定它们曾真实生效；
9. 真实 Postgres 克隆和目标多进程环境验证、独立人工复核。

## 8. FS-001 当前状态

**PARTIALLY REMEDIATED / PROFILE APPLY & ROLLBACK FAIL-CLOSED / RUNTIME ACTIVATION OPEN。**

已关闭：profile apply/rollback 的 HTTP 错误成功、虚假 recommendation 终态和本入口的
新写入副作用。

未关闭：真实 apply、真实 rollback、worker readback、历史漂移对账和目标环境验证。
因此 FS-001 继续是 P1 HARD BLOCKER，G2 仍未放行。

## 9. 生产决定

**REAL-MONEY PRODUCTION: NO-GO**

本记录不是部署授权、交易许可或上线批准。没有执行任何 live-funds 操作，也没有读取或
展示凭证、账户、订单、仓位或余额。
