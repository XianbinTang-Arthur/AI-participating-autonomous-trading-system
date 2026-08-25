# FS-008 PostgreSQL 连接预算整改证据

> 文档状态：Phase 3U 声明拓扑预算与静态防回退已实施；目标负载、故障恢复、瞬时调用与内存预算开放  
> 最后核对：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上未提交 Phase 3A–3V 叠加变更  
> 运行时边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动服务、Docker 或部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 整改前事实

Phase 1/2 静态审计得到以下上限：四个主 runtime storage 均为 15+45，合计 240；计入
RDP、live query/facts/session、governance、两个 collector 和 orderbook 后，稳态理论值约
317；再计四进程 active-parameter transient startup 为 321。PostgreSQL 声明上限 200，
superuser reserved 3，普通连接容量约 197。

该算术证明系统没有全局预算，但不证明所有 overflow 会同时占满。Phase 2 只读样本为 40，
可信生产峰值仅估计约 142–160，且不是完整 live topology，因此 `FS-008` 降为 P2 容量风险，
仍是上线条件项。

## 2. Phase 3U 实现

### 2.1 单一预算真源

新增 `aats/storage/connection_budget.py`，定义：

- PostgreSQL 容量 200、reserved 3、最低运营余量 40；
- gateway/market/decision/execution/monolith 五种主 pool；
- RDP research、live query、live facts、live session RW/RO、governance cached/transient、
  Gateway governance API、active-parameter transient 和 orderbook read pool；
- 14 个声明 live topology component、总 ceiling 和名义余量算术；
- 非负配额、正实例数和未知角色失败关闭。

四进程主存储 pool 从统一 60 改为角色化 32/8/10/16，合计 66。完整声明拓扑如下：

| 组件 | 实例 | 单实例 ceiling | 合计 |
|---|---:|---:|---:|
| primary gateway | 1 | 32 | 32 |
| primary market | 1 | 8 | 8 |
| primary decision | 1 | 10 | 10 |
| primary execution | 1 | 16 | 16 |
| RDP research | 1 | 15 | 15 |
| RDP live query | 1 | 8 | 8 |
| RDP live facts | 1 | 8 | 8 |
| RDP live session RW | 1 | 5 | 5 |
| RDP live session RO | 1 | 4 | 4 |
| RDP governance cache | 1 | 5 | 5 |
| live collector RDP | 2 | 15 | 30 |
| execution orderbook read | 1 | 2 | 2 |
| active-parameter startup | 4 | 1 | 4 |
| Gateway governance API | 1 | 3 | 3 |
| **合计** |  |  | **150** |

普通连接容量为 197，因此声明 topology 留 47 个名义 slots，高于自动门槛 40。monolith 的
32 是四进程主角色的替代项，不与它们同时相加。

### 2.2 角色传递和模块收敛

`create_database_runtime` 新增 optional keyword-only `process_role`，并从单一真源解析配额；
bootstrap 把当前进程角色显式传入。`None` 兼容 monolith，未知角色非零失败。

RDP、live query、live facts、RW/RO live session、governance、Gateway governance API、
active parameter 和 orderbook read 的裸 pool 数字均改为引用单一真源。短命 CLI 与 missed
market replay 改用 `NullPool`，避免命令结束前维持额外 QueuePool。

### 2.3 Compose 与自动防回退

Compose 现在同时显式声明 `max_connections=200` 与
`superuser_reserved_connections=3`。新增标准库 verifier：

1. 校验声明 ceiling、最低余量和 component 唯一性；
2. AST 扫描全部 `aats/**/*.py`，要求 13 个 `create_engine` 调用全部归类；
3. 九个 QueuePool 所在文件必须使用精确批准的 budget root，两个短命 engine 必须
   `NullPool`；
4. 主 storage 必须经 `primary_storage_pool_limit(process_role)` 解析；
5. Compose 容量必须与代码一致；
6. GitHub quality workflow 必须在安装第三方依赖前运行该 verifier。

AST 读取使用 `utf-8-sig`，确保仓库中带 BOM 的 Python 源文件不会绕过全量 inventory。

## 3. 当前验证结果

| 检查 | 结果 | 可信边界 |
|---|---|---|
| connection budget verifier | `declared_ceiling=150 operational_reserve=47 components=14 engine_calls=13` | 纯静态；无数据库 I/O |
| FS-008 contract | `12 passed` | 含 SQLAlchemy sync/async factory 别名绕过对抗样例 |
| FS-008 + runtime/orderbook/config generator focused | `56 passed` | 本机 Python 3.14；外部服务均 mock/未启动 |
| connection/dependency verifiers | `ceiling=150 reserve=47 components=14 engine_calls=13`; `runtime=46 ci=33 images=9` | 标准库静态契约；无网络/数据库 I/O |
| unit strict marker collection | `4423 tests collected` | 无 unknown unit marker |
| 标准完整 unit（唯一 basetemp） | `4393 passed, 30 skipped, 1659 warnings, 85 subtests passed in 115.81s` | 完整 `tests/unit/`；无外部服务成功声明 |
| 应用 Ruff `--fix` / 全仓 Ruff | `All checks passed!` / `All checks passed!` | `.venv` Ruff 静态检查 |
| Python environment consistency | `No broken requirements found` | 本机开发 venv，不是目标 lock install |
| workflow/Compose YAML | `YAML OK: 8 files` | PyYAML 语法解析，不是 Compose runtime validation |
| Markdown 本地链接 | `787 files / 1031 relative local targets OK` | 忽略 URL、anchor、代码块和本机绝对路径；不验证外部 URL/anchor |
| diff whitespace | `git diff --check` 通过 | 仅有既有 LF→CRLF 提示，无 whitespace error |
| WSL2 integration/目标负载/故障注入 | **未执行** | 需要隔离 PostgreSQL/完整 daemon 拓扑与人工批准 |

仓库要求的原样完整 unit 命令先运行，在 87 项通过后因 Windows 用户 temp 根目录 ACL 于
`tmp_path` setup 报 `PermissionError`，没有业务断言失败；随后使用仓库内本次唯一
`--basetemp` 重跑同一完整范围并得到上表结果。定向组合的原样运行也由相同 ACL 在
36 项通过后阻断，唯一 basetemp 重跑为 56 passed。

没有执行 WSL2 integration、Docker、PostgreSQL 参数/连接采样或目标负载；上表静态与
mock 结果不得表述为运行容量通过。

## 4. 威胁模型与失败姿态

整改前，Gateway fan-out、execution 对账、RDP/collectors 和慢查询可让独立 pool 同时扩张，
普通连接接近 197 后控制面、恢复和 admin 连接可能超时或拒绝。Phase 3U 把当前声明上限压到
150，并保留名义余量，使单个主进程不能再占用 60。

但该措施不提供跨进程实时 semaphore。数据库仍按实际连接接受请求，且以下路径未被 150
严格封顶：

- governance transient engine 的并发实例数；
- `NullPool` CLI/replay 的并行启动数；
- Alembic/schema、恢复、admin、诊断和仓库外进程；
- topology 与声明实例数漂移；
- 连接泄漏、慢事务、进程重启重叠与网络故障后的重连峰值。

因此 47 是设计余量，不是已证明的故障余量。降低 pool 还可能把问题表现为更早的 30 秒
pool timeout 或 Gateway p95/p99 上升；这比无界争抢更可控，但仍需 SLO 和降级姿态验证。

## 5. 未关闭项与关闭标准

当前裁定：
`PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN`。

关闭 `FS-008` 至少需要：

1. 在无真实交易所写入的生产等价隔离栈启用全部目标 daemon/collector/RDP；
2. 明确并重放 Gateway fan-out、execution/reconciliation、RDP 查询和 collector 的目标并发；
3. 注入慢查询、idle-in-transaction、数据库短断、连接重建和进程滚动重启；
4. 测量每服务 checked-out/overflow/wait/timeout、PostgreSQL active/idle/rejected、恢复/admin
   余量、query p95/p99 和恢复时间；
5. 建立运行时 pool/数据库利用率告警及阈值，并验证告警送达；
6. 盘点或限制 transient、CLI、迁移、恢复和仓库外 engine 实例；
7. 对 `work_mem=64MB`、shared buffers、连接数和 2.5 GiB 容器做联合内存预算/故障验证；
8. 独立 reviewer 复核 topology 假设、负载模型、阈值和证据。

在上述证据完成前，不能把静态 ceiling=150 写成“数据库容量已验证”，也不能授权 live、
部署或真实资金上线。详细设计和验收契约见
[`../../docs/task/fs_008_database_connection_budget_sow_2026_08_25.md`](../../docs/task/fs_008_database_connection_budget_sow_2026_08_25.md)。
