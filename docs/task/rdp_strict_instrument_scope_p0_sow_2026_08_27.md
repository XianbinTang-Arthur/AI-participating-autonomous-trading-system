# RDP 严格 Instrument Scope 分类 P0 任务书

> 文档状态：现行实施任务书
> 最后核对：2026-08-27（起始基线 `main@e3d1668`，以当前工作树为准）
> 核对范围：静态代码与聚焦单元测试；不证明数据库、容器、交易所或实盘状态
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

关闭跨 RDP lineage、Gold、replay、capital eligibility、治理 API 与官方历史导入的
instrument scope 误分类 P0。只有现有明确支持集中的 BTC/ETH spot 与 swap 可以分别进入
`spot`/`swap` 路径；空值、未知币对、仅具有 `-SWAP` 后缀的未知产品及 FUTURES 等其他产品
统一为 `unsupported/unproven`，不得因“不是 swap”而被推断为 spot。

不扩展支持集合，不修改 observation-window 或 Decimal 算术，不迁移数据库，不部署，
不接触 live profile、订单、资金、参数或凭证。Research Factory `real_data.py`
属受影响边界：它必须复用同一严格 scope 分类，并在 derivative lineage verifier 不可用时
失败关闭。

## 2. 模块职责与领域模型

领域层提供唯一纯函数 `classify_instrument_scope`，以显式 allowlist 返回 `spot`、`swap` 或
`unsupported`；稳定失败码为 `instrument_scope_unsupported_or_unproven`。数据平台旧入口只做
兼容适配，不复制后缀判断。lineage 与 capital eligibility 把 unsupported 记录为不合格证据；
Gold、replay、campaign planner、funding/candle deep backfill 与导入入口在查询、网络或写库前
失败关闭；治理 SQL 使用相同 allowlist 参数。

## 3. 输入/输出接口

- 输入：任意待分类 symbol；标准化只允许 trim 与 uppercase。
- 输出：`spot | swap | unsupported`。
- 支持集：`BTC-USDT`、`ETH-USDT`、`BTC-USDT-SWAP`、`ETH-USDT-SWAP`。
- unsupported 边界：空值、`DOGE-USDT`、`DOGE-USDT-SWAP`、`BTC-USDT-240927` 等均不得进入
  spot/swap 数据表、衍生品 lineage 或资本资格路径。

## 4. 数据库、事务、一致性与并发

不改 schema、表、索引或约束。治理 API 的 PostgreSQL 只读聚合以绑定参数传入支持集合；官方
导入在开启 session、创建 ingest run 或网络请求前完成分类。无新增事务、锁或并发状态。

## 5. 授权、认证与数据安全

本切片不新增权限或外部访问，不读取 `.env.*` 或凭证。分类失败不会触发网络、数据库写入、
部署或交易副作用。

## 6. 错误处理、幂等与生命周期

unsupported 在证据型与执行型接口均保留稳定码
`instrument_scope_unsupported_or_unproven` 并停止或判为不合格。相同标准化输入得到确定性分类；支持集合
变化只能通过代码审查修改领域常量并同步回归。

生命周期为：输入标准化 → 显式集合分类 → supported 路由或 unsupported 阻断。不得加入
suffix、正则形状或 non-swap fallback。

## 7. 缓存、性能、日志、监控与审计

分类仅做四项集合查找，无缓存或外部依赖。现有日志/审计保留 symbol 与稳定 reason code；不得
记录来源 payload 或敏感配置。治理 API 继续使用有界只读查询。

## 8. 测试策略

覆盖大小写/空白兼容、明确 spot/swap、未知普通币对、伪 `-SWAP`、FUTURES 与空值；验证 lineage
和 capital eligibility 不合格、Gold/replay 在查询前阻断、官方导入在副作用前拒绝，以及治理
SQL 不再使用通用 symbol 形状正则。运行对应聚焦测试与目标 Ruff。

## 9. 迁移、回滚与兼容

无数据迁移。明确支持的四个 symbol 及现有 `instrument_type_for_symbol` 调用保持兼容；历史上
依赖未知 symbol 自动落入 spot 的调用会按 P0 要求失败。代码回滚会恢复不安全推断，因此只有
在替代的严格分类实现存在时才可回滚本切片。

## 10. 配置、环境、代码组织与依赖

支持集合属于代码真源，不新增 YAML、环境变量、managed-profile 开关或第三方依赖。领域 helper
不得依赖 DB、网络或 service runtime；RDP 模块只消费该 helper。

## 11. 文档、运维、部署与验收

无需新增运维步骤或部署。验收条件：指定生产代码路径无 suffix/non-swap 推断；四个明确支持
symbol 行为不变；所有未知/FUTURES 失败关闭；聚焦测试与 Ruff 通过；独立复审无 P0/P1；
未经 WSL2/模拟环境运行验证不得声明运行态完成，本切片不授权部署或任何 live 副作用。

2026-08-27 本地验收已纳入 P0-B 联合证据：Windows 全量 unit 为
`5048 passed, 30 skipped, 94 subtests passed`，本轮目标 Ruff 通过，独立最终复审结论为
`ACCEPT` 且无未关闭 P0/P1。FS009 仅完成 `5 tests collected`；未运行真实 PostgreSQL、
WSL2 E2E 或部署，因此仍只构成静态/单元验收，不构成运行态或上线验收。
