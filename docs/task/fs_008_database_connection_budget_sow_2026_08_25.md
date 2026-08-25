# FS-008 PostgreSQL 连接预算设计与实施范围

> 文档状态：Phase 3U 声明拓扑预算与静态防回退已实施；目标负载、故障恢复、瞬时调用与数据库内存预算开放  
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3V 整改  
> 核对范围：SQLAlchemy engine 创建点、主运行进程角色、RDP/collector/governance/orderbook pool、WSL2 Compose PostgreSQL 容量、CI 静态契约与单元测试  
> 运行时边界：不读取 `.env.*`，不连接 PostgreSQL、Redis、NATS、交易所或账户，不启动 Docker 或服务，不部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段处理 `FS-008` 中可由仓库代码安全收口的连接预算缺口：把分散的 SQLAlchemy
`pool_size`/`max_overflow` 收敛为单一预算真源；按 gateway、market、decision、execution
和 monolith 角色分配主存储池；显式声明四进程 live 拓扑的 pool ceiling、PostgreSQL 普通
连接容量和恢复余量；让新的或未归类的 engine 创建点在 CI 中失败。

本阶段不是容量验收。声明 pool ceiling 是配置允许上限，不是同时占用量、排队时延或目标
负载实测。治理层按调用创建的 transient engine、短命 CLI/回放、迁移、恢复、人工诊断和
仓库外调用仍可能叠加；PostgreSQL `work_mem=64MB` 与 2.5 GiB 容器的算子级内存风险也未
解决。因此本阶段只能降低和约束静态风险，不能证明生产等价拓扑不会耗尽连接或内存。

## 2. 整改前行为与根因

整改前四个主 runtime 进程均复用 `pool_size=15`、`max_overflow=45`，每进程 ceiling 为 60，
四进程即为 240，已经超过 PostgreSQL `max_connections=200`。再计入 RDP、live query、
live facts、RW/RO live session、governance、两个 collector、orderbook read 和 active
parameter startup，稳态理论上限约 317、启动瞬时约 321；迁移/admin 尚未计入。

Phase 2 对抗复核同时确认 overflow 按需创建，生产可信峰值仅能估计为约 142–160，未证明
一定超过普通连接容量 197，因此 finding 从 P1 降为 P2。真正根因仍成立：各模块各自定义
上限，没有全局算术、角色差异、恢复余量或新增 engine 的自动审查门禁。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `aats/storage/connection_budget.py` | 连接容量、角色配额、组件实例数、声明 ceiling 与余量的单一真源 |
| `aats/storage/session.py` | 按 `process_role` 解析主存储池，并保留 30 秒 pool timeout 背压 |
| `aats/bootstrap/config.py` | 将已解析的进程角色传入主数据库 runtime |
| RDP/live/governance/orderbook 模块 | 从单一真源引用其已审 pool，不再维护裸整数 |
| `aats/cli.py`、missed-market replay | 短命数据库操作使用 `NullPool`，不保留独立 QueuePool |
| `deploy/wsl2-dev/docker-compose.yml` | 显式声明与预算一致的 `max_connections=200` 和 reserved=3 |
| `scripts/verify_database_connection_budget.py` | 无数据库 I/O 地验证容量算术、engine inventory、pool 来源、Compose 和 CI |
| FS-008 contract tests | 对角色配额、总量、未知角色失败关闭、调用透传与门禁接入防回退 |

`ConnectionPoolLimit` 的 ceiling 定义为 `pool_size + max_overflow`；
`DeclaredPoolComponent` 再乘以期望实例数。该模型只描述已声明的 QueuePool 最大并发连接，
不把 thread 数量、session 数量或 query 数量错误等同为数据库连接数。

## 4. 输入、输出与接口

`create_database_runtime(database_url, *, process_role=None)` 新增可选关键字参数。
`None` 保持 monolith 兼容；显式角色经 trim/lower 解析。支持值为 `gateway`、`market`、
`decision`、`execution`、`monolith`；未知或拼写错误角色抛出 `ValueError`，不静默回退到
较大的 pool。

角色化主池如下：

| 角色 | pool_size | max_overflow | ceiling |
|---|---:|---:|---:|
| gateway | 12 | 20 | 32 |
| market | 4 | 4 | 8 |
| decision | 5 | 5 | 10 |
| execution | 8 | 8 | 16 |
| monolith | 12 | 20 | 32 |

monolith 是四进程 topology 的替代运行形态，不与 gateway/market/decision/execution 同时
计入声明总量。Gateway 查询线程上限可以高于 32；超出连接池的工作应等待并最终在
`pool_timeout` 失败，而不是让该进程扩张到原有 60。

## 5. 数据库 schema、表、索引与约束

无 migration、schema、表、列、索引、约束或持久化 payload 变更。Compose 仅把 PostgreSQL
已采用的 `max_connections=200` 与 `superuser_reserved_connections=3` 显式纳入静态一致性
校验。本阶段没有读取目标数据库当前参数，不能证明已运行容器与 Compose 一致。

## 6. 事务、一致性与并发

本阶段不改变事务边界、session 生命周期、isolation level 或 OrderState 三层持久化语义。
降低 pool ceiling 后，过载表现会更早转换为等待/timeout，目的是保留 PostgreSQL 全局
余量，而不是承诺请求永不超时。

四进程声明拓扑为：

| 组件 | ceiling |
|---|---:|
| 四个主存储角色 | 66 |
| RDP research | 15 |
| live query | 8 |
| live facts | 8 |
| live session RW/RO | 9 |
| governance cached | 5 |
| 两个 live collector 独立 RDP pool | 30 |
| execution orderbook read | 2 |
| 四进程 active-parameter startup | 4 |
| Gateway governance API | 3 |
| **声明合计** | **150** |

PostgreSQL 普通连接容量为 `200 - 3 = 197`，名义余量为 47，高于代码要求的最低 40。
这 47 是预算缓冲，不是可自由分配的新业务配额；它用于容纳未建模瞬时、迁移、恢复、
admin 和误差，并仍需通过观测与故障压测验证。

## 7. 授权、认证与数据安全

- 静态 verifier 不读取环境变量、`.env.*`、数据库 URL 或凭据；
- 本阶段不连接数据库，不执行查询、DDL、迁移或账户操作；
- 预算值和 engine 文件清单可进入日志，连接串、用户名和 secret 不得进入；
- 后续目标压测必须使用无真实交易所写入的隔离环境，并由人工批准；
- 不得为了通过容量测试而关闭 kill switch、live gate 或数据库 reserved slots。

## 8. 错误处理与幂等

以下漂移必须导致 verifier 非零失败：

1. 声明余量低于 40；
2. Compose 容量与代码单一真源不一致；
3. 新增、删除或移动 `create_engine` 调用而未更新审查 inventory；
4. QueuePool 调用缺少 `pool_size`/`max_overflow`；
5. pool 参数使用裸整数、错误属性、未批准 pool root 或未从单一真源导入；
6. 短命 engine 不使用 `NullPool`；
7. 主存储 pool 不再由 `process_role` 解析；
8. CI 不再执行数据库连接预算 verifier。

静态校验只读文件，重复执行幂等。未知角色失败关闭，防止拼写错误绕过到默认大池。

## 9. 状态转换与生命周期

```text
process role resolved
  -> primary_storage_pool_limit(role)
  -> SQLAlchemy QueuePool constructed with reviewed ceiling
  -> request/session checks out a connection
  -> pool exhausted: bounded wait at pool_timeout
  -> timeout/error handled by existing caller posture
  -> session close/rollback returns connection
```

CI 生命周期为：扫描全部 `aats/**/*.py` AST，建立 engine inventory，校验 QueuePool/NullPool
策略和单一真源，再校验 Compose 算术。任何新增 engine 必须先声明生命周期、实例数和容量
影响，不能通过复制一个已批准常量来规避拓扑评审。

## 10. 缓存与性能

Gateway ceiling 从 60 降为 32，market/decision/execution 分别降为 8/10/16。此变更会降低
慢查询或 fan-out 高峰时数据库被单个进程吞噬的能力，但可能增加 pool wait、timeout 和
API p95/p99。没有负载数据时不能声称性能改善。

后续测试必须分别观测 SQLAlchemy checked-in/checked-out/overflow、pool wait/timeout，
PostgreSQL active/idle/idle-in-transaction、拒绝连接、query latency、内存和恢复任务完成
时间。现有 idle-in-transaction server timeout 是安全网，不替代慢查询和 session 泄漏治理。

## 11. 日志、监控与审计

verifier 成功只输出 declared ceiling、operational reserve、组件数和 engine 调用数，不输出
连接信息。运行时仍缺少按服务的 pool utilization、wait 和 timeout 统一指标/告警；这属于
FS-008 未关闭项。

后续容量证据至少应记录 commit/image ID、profile、进程拓扑、数据库参数、数据规模、负载
模型、故障注入、每服务连接曲线和判定阈值。单次 `pg_stat_activity` 快照不能替代峰值与
恢复阶段时间序列。

## 12. 测试策略

仓库内对抗测试覆盖：

1. 五个主角色的精确配额和未知角色失败关闭；
2. 14 个声明 topology component 合计 150；
3. 普通容量 197、名义余量 47 且不低于 40；
4. `create_database_runtime` 按角色把精确参数传入 SQLAlchemy；
5. 13 个应用 `create_engine` 调用全部归类；
6. QueuePool 参数来自批准的连接预算 root，两个短命调用使用 `NullPool`；
7. Compose 容量与 CI verifier 接入；
8. 既有数据库 runtime 与 orderbook snapshot 契约无回归。

仍需在 WSL2/目标 Postgres 的隔离栈完成全 daemon 负载、慢查询、短断、进程重启、恢复/admin
竞争与 soak。未执行这些测试时，`FS-008` 不能 CLOSED。

## 13. 迁移、回滚与兼容

无数据库迁移。`process_role` 是可选 keyword-only 参数，默认 monolith，保留已有直接调用的
API 兼容。标准 bootstrap 显式传入角色。

若目标验证显示 pool 过小，必须基于观测同时重算全局 ceiling 和保留余量，再调整单一真源、
测试、Compose/基础设施计划与审计记录；不得在业务模块临时提高裸整数。回滚角色化配额会
恢复原有全局超配风险，不能作为无审查的紧急性能修复。

## 14. 配置与环境隔离

预算值是当前 WSL2 Compose topology 的代码约束，不是可由 `.env.*` 任意覆盖的参数。
这是为了让 pool 变更进入代码审查并自动重算。不同部署拓扑若需要不同容量，必须建立显式、
受版本治理的 profile 预算，不能依赖隐式环境变量。

本阶段没有验证已运行 Postgres 的真实 `max_connections`、reserved、连接数或资源限制。
文档中的 200/3 只证明当前 Compose 声明和代码常量一致。

## 15. 代码组织与依赖

单一真源放在 storage 层，不放到 API、Compose parser 或文档。所有 application
`create_engine` 创建点继续由原模块拥有其生命周期，但 pool 数字必须引用该真源。verifier
只使用 Python 标准库加项目常量，以便在 CI 安装第三方包前运行；它不 import 业务服务、
不会触发 managed profile 或数据库连接。

`GOVERNANCE_TRANSIENT_ENGINE_POOL` 已有单次上限，但因调用并发实例数尚未建立可靠界限，
未伪装进声明 topology 150。CLI/replay 使用 `NullPool` 也只消除持久池，不限制并发启动数。
这些路径需要后续生命周期盘点或进程级 semaphore/共享 pool 设计。

## 16. 文档、运维手册与验收标准

Phase 3U 仓库内验收标准：

- 单一预算模块覆盖所有当前 QueuePool 创建点；
- 四进程声明 topology ceiling 从历史 317/321 收敛为 150；
- Compose 普通连接容量 197、名义余量 47，最低门槛 40 自动失败关闭；
- 新 engine、裸 pool 值、错误 pool root、短命持久池和 CI 移除均被 contract 阻断；
- focused tests、全仓 Ruff、完整 unit、文档链接、YAML 与 diff check 通过；
- 未执行的目标负载、故障、内存、告警与独立复核明确登记为 OPEN；
- 真实资金生产继续 `NO-GO`。

当前裁定：
`PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN`。

实施证据见
[`../../audit/full_system_2026_08_24/41-fs-008-database-connection-budget.md`](../../audit/full_system_2026_08_24/41-fs-008-database-connection-budget.md)。
