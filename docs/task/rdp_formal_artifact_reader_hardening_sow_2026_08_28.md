# RDP 正式 Artifact 有界稳定读取加固任务书

> 文档状态：实施任务与静态验收记录
> 最后核对：2026-08-28（起始 HEAD `c15ccd2d5057`，以本文档所在提交为交付基线）
> 核对范围：正式 research artifact 文件读取合同、直接消费者与隔离测试；不证明现场数据库、容器或交易状态

## 1. 业务目标与边界

正式研究证据在进入候选导入、晋级资格或收益证据判断前，必须从一个已打开的普通文件描述符完成有界读取，并拒绝读取期间的路径替换、文件变化、符号链接、超限内容、非 UTF-8、重复 JSON key 和非有限数值。该任务不改变研究阈值、候选选择、数据库快照发布或任何 live 行为。

## 2. 模块职责与领域模型

`research_artifact_contract.py` 负责文件身份、容量与 JSON 语法安全；候选导入、parameter lineage、Phase 6 qualification 和 evidence bundle 只消费该统一原语。manifest/digest 继续负责内容身份，reader 不把路径存在性升级为业务可信度。

## 3. 输入输出接口

- bytes reader：输入直接子文件、父目录和正整数容量上限；输出同一描述符读得的 bytes，异常失败关闭。
- JSON decoder/reader：输入 bytes 或上述文件；输出严格 JSON 值及原始 bytes；可要求顶层 `dict`/`list`。
- 现有 `require_regular_round_file` API 保留，内部升级为有界稳定读取。

## 4. 数据库、表、索引与约束

不新增或修改数据库对象。数据库 managed snapshot 与 typed JSON digest 约束保持不变。

## 5. 事务、一致性与并发

单次读取只使用一个打开的文件描述符；读取前的路径身份、描述符读取前后状态和读取后的路径身份必须一致。该机制不提供跨进程写锁，**写方使用 immutable/atomic-replace publication 是强制前置条件**；禁止对正式文件原地并发改写后恢复大小/mtime。语义消费者还必须复核 manifest digest 或 managed snapshot，不能把 reader 单独当成内容信任根。

## 6. 授权、认证与数据安全

不扩大可读取根目录，不读取凭证。正式 round 仍受既有 canonical layout、relative-ref、digest 和 managed DB 真值约束。

## 7. 错误处理与幂等性

路径异常、非普通文件、超限、读取失败、身份变化或严格 JSON 失败均返回既有调用方可失败关闭的 `ValueError`/`None` 结果；重复读取不产生写入。

## 8. 状态迁移与生命周期

无新状态。非法或不稳定 artifact 不能进入 `qualified`、`bound` 或正式候选导入状态。

## 9. 缓存与性能

JSON 正式证据默认限制为小型有界文件；回测非 JSON 证据允许更高但固定的上限。分块读取避免一次无界系统调用，返回 bytes 的既有消费者行为保持兼容。

## 10. 日志、监控与审计

沿用调用方现有错误原因和 warning；不记录 artifact 正文或敏感数据。稳定 reader 提供可分类的路径、超限、读取和变化错误码。

## 11. 测试策略

覆盖正常 bytes/JSON、超限、重复 key、NaN/Infinity/浮点溢出、非法 UTF-8、打开前替换和读取中替换；再运行候选导入、晋级资格和 evidence bundle 的相关回归。

## 12. 迁移、回滚与兼容

无需数据迁移。回滚为恢复 reader 与调用方变更；由于 API 保留，调用代码无需版本迁移。超过新上限的旧 artifact 将明确失格而非被截断或部分解析。

## 13. 配置与环境隔离

容量上限为代码级安全合同，不从易漂移环境变量覆盖；Windows 和 WSL2 均使用 Python 文件描述符语义并在缺少 `O_NOFOLLOW` 时依靠 `lstat/fstat` 身份复核。

## 14. 代码组织与依赖

只使用标准库 `os/stat/json/hashlib/pathlib`；统一实现位于 governance contract，调用方不复制新的文件读取器。

## 15. 文档与操作手册

本任务书记录实施边界。由于不改变操作入口、部署命令或 UI，不修改运行手册。

## 16. 部署与回滚

本切片不部署、不启动服务、不触发 live。后续只能随已提交、完整验证的 RDP 候选通过标准 derivatives 模拟部署入口验证。

## 17. 验收条件

1. 正式 bytes/JSON 读取均有硬容量上限并从单一描述符读取；
2. 路径/文件在读取窗口变化时失败；
3. JSON 重复 key、非有限数值、非法 UTF-8 和错误顶层类型失败；
4. 候选导入、lineage、promotion qualification 与 backtest evidence 消费者使用统一原语；
5. 窄范围测试、Ruff 与 `git diff --check` 通过；
6. 本 reader 切片不修改 snapshot publication 行为；本轮未部署、无 live 副作用。

## 18. 当前验证证据

截至 2026-08-28，本地候选的专门 reader 测试 `15 passed`，相关制品/参数身份/晋级聚焦回归
`342 passed, 1 skipped`，Windows 全量单元测试 `5924 passed, 31 skipped`，Step 3 独立契约脚本
`145 passed`，WSL2 隔离 PostgreSQL 相关集成 `28 passed`；相关 Ruff 与 `git diff --check` 通过。
独立增量复审未发现 P0/P1。本证据不代表部署、现场迁移、衍生品 LF-B producer 完成或 live 可用。
