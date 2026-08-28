# RDP 类型敏感 JSON 不可变身份 P1 整改任务书

> 文档状态：本地实施候选；全量单元、真实 PostgreSQL 与独立复审已通过，尚未部署
> 最后核对：2026-08-28（起始 HEAD `c15ccd2d5057`，叠加当前 RDP 控制面候选；以本文档所在工作树为准）
> 真实性边界：描述本次代码与迁移目标，不代表目标数据库已迁移，也不代表 RDP、模拟盘或实盘运行就绪。

## 1. 业务目标与边界

消除 PostgreSQL JSONB 以及 Python 容器相等比较把 JSON number `1` 与 `1.0` 判等导致的不可变身份绕过，覆盖参数集、研究轮次快照、决策轮次快照，以及 Step 3 producer/importer 的文件—DB 锚点关系。`-0.0` 与 `0.0` 保持等价，object key 顺序不参与身份。范围不包含部署、live 参数应用、订单或资金副作用。

## 2. 模块职责与领域模型

`typed_json_identity.py` 是严格 JSON canonical 编码单一真源；三类治理写入层负责在跨越 JSONB/Text 边界前计算摘要，并把业务主键解释为 insert-once 身份。Step 3 producer 和 managed importer 也用同一摘要验证解析 payload 的跨读取、跨介质关系；原始文件仍由 byte SHA-256 与 size 绑定。生命周期字段仍由各自专用事务维护。

## 3. 输入与输出接口

输入只接受 `null/bool/int/finite float/string/list/string-key object`。输出是 64 位小写 SHA-256。现有公开写入函数签名不变；冲突继续抛出原有 `DBConflictError` 分类。

## 4. 数据库表、索引与约束

在 `governance.parameter_sets`、`governance.research_round_snapshots`、`governance.decision_round_snapshots` 增加可空 `typed_json_identity_sha256 VARCHAR(64)`，并以 CHECK 限定格式。无需索引；摘要只在主键冲突路径比较。

## 5. 事务、一致性与并发

INSERT 和冲突校验保持单条 PostgreSQL 语句。新行原子写入摘要；旧行仅在标量身份相同、JSONB canonical text 相同且摘要为空时由 exact retry 原子补写。已有非空摘要必须完全相同。

## 6. 授权、认证与数据安全

不新增 API、权限或凭证读取。摘要不包含秘密的明文输出，只用于数据库内部一致性比较。

## 7. 错误处理与幂等

非严格 JSON、非有限浮点或非字符串 key 失败关闭。`1`/`1.0` 冲突；负零/正零及 key 重排为合法幂等重试。数据库冲突不产生部分更新。

## 8. 状态转换与生命周期

本整改不改变 parameter set、research round 或 decision round 生命周期；只收紧同一业务 ID 背后的内容身份。

## 9. 缓存与性能

每次写入增加一次对有界治理 payload 的 canonical serialization 与 SHA-256；无缓存失效或查询路径额外开销。

## 10. 日志、监控与审计

沿用现有 insert/verify 日志及冲突错误码。可通过摘要列为空率识别尚未发生 exact retry 的历史记录，但不得把空值解释为内容无效。

## 11. 测试策略

单元测试验证严格 JSON、类型差异、负零、key 顺序，以及 Step 3 producer/importer 在文件—DB 边界发生 `1`/`1.0` 类型换档时失败关闭；SQL/ORM 测试验证三表迁移契约；真实 PostgreSQL 集成测试分别覆盖三类写入的冲突与幂等语义。

## 12. 迁移、回滚与兼容

Batch B 20 仅增列和 CHECK，历史行保持可读且不臆造摘要。首次 exact retry 安全补写；rollback 只删除新增列。回滚会失去持久摘要保护，必须在停写窗口执行。

## 13. 配置与环境隔离

不新增配置。Windows 运行单元测试；PostgreSQL 集成测试仅使用隔离 Testcontainers/WSL2 环境，不读取 `.env.*`。

## 14. 代码组织与依赖

复用标准库 `json/hashlib/math`，不新增第三方依赖。现有 `parameter_values_fingerprint` 委托统一 encoder，保持已发布 schema discriminator 和摘要结果兼容。

## 15. 文档与操作手册

本任务书记录设计和验收边界；Batch B runner 与 ORM 是当前 schema 真源。没有运行迁移证据前，不得宣称现场列已存在。

## 16. 部署与验收标准

验收要求：lint 通过；相关单元测试通过；Batch B 全链迁移/回滚契约通过；真实 PostgreSQL 中三类对象均满足 `1`/`1.0` 冲突、负零 exact retry、key-order exact retry；独立复审无未关闭 P0/P1。本任务不授权部署、push 或 live 操作。

## 17. 当前验证证据

截至 2026-08-28，本地候选已取得：Step 3 identity binding 定向测试 `59 passed`，相关 typed identity/研究快照/Step 3 合同测试 `53 passed, 1 skipped`；WSL2 隔离 PostgreSQL promotion identity 整文件 `17 passed`；Batch B 全链幂等、末阶段回滚和修复 `1 passed`；相关 Ruff 与全工作树 `git diff --check` 通过。该证据不等于现场迁移、部署或运行验证。
