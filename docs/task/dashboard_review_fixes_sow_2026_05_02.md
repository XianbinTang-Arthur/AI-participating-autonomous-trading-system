# Dashboard Review Fixes SOW - 2026-05-02

## Business objectives and boundaries

修复本轮 UI 性能重构代码审查暴露的四个问题：局部刷新污染整页 freshness、recentDecisions 未预热变体清空列表、Trial Review 明细抽屉字段映射错误、panel 请求超时后底层同步查询继续堆积。边界限定在 dashboard bundle、snapshot 读取、前端局部刷新与抽屉渲染，不改变交易执行、风控决策或外部 API 契约。

## Module responsibilities and domain model

- `dashboard-refresh.js` 负责前端刷新生命周期；局部 panel 刷新只能影响 panel loading/error/data，不代表 view primary bundle 已完成。
- `dashboard_snapshot.py` 负责物化快照；未物化参数变体允许后台补热，但不能把默认空 payload 当成真实业务空状态覆盖用户已看到的数据。
- `auth_routes.py` 负责 bundle 聚合、snapshot/live fallback、panel 超时与认证摘要。
- `report-drawers.js` 负责按需明细抽屉的展示层字段映射，不改变后端报表结构。

## Input/output interfaces

保持 `/dashboard/bundle`、`/reports/trial-review-details`、`/decision/recent` 等现有 URL 和 JSON 字段兼容。新增逻辑仅改变内部 fallback：未物化 `recentDecisions` snapshot miss 时返回 live 查询结果或 panel timeout，而不是 snapshot 默认空列表。

## Database schema / tables / indexes / constraints

不涉及数据库 schema、索引、约束或迁移。

## Transactions, Consistency, Concurrency

对同步 panel 查询增加请求级并发闸门。panel 超时返回后，底层线程仍不可强制取消，但闸门 slot 会保持到线程实际结束，防止自动刷新/重试继续堆积同类 DB 查询。

## Authorization, Authentication, Data Security

不读取或输出任何密钥、密码或 token。现有 session/API key 认证流程保持不变。

## Error Handling and Idempotency

局部刷新失败仍只标记目标 panels。未物化 snapshot miss fallback 到 live 后，live 异常仍按 panel-level error 返回，不影响其他 panels。超时返回统一 `dashboard_bundle_panel_timeout`。

## State Transition and Lifecycle

局部刷新不更新 `readyViews` 与 `viewRefreshedAt`，避免把单 panel 结果提升成整页 freshness。snapshot miss 会触发后台 refresh，live fallback 只服务当前请求。

## Caching and Performance

保留 dashboard bundle 短 TTL cache、snapshot plane 与 deferred bundle。新增并发闸门防止超时线程积压；不扩大默认 snapshot 预热范围，避免为了修复 `recentDecisions` 而增加启动负担。

## Logging, Monitoring, Auditing

超时后继续运行的后台 panel task 完成时释放 slot；如果后台 task 异常，记录 warning 日志用于排障。

## Testing Strategy

补充窄测试覆盖：
- `refreshPanels` 不写 view-level freshness。
- 未物化 `recentDecisions` 变体 miss 时 fallback 到 live。
- Trial Review 明细抽屉读取 `sections.forward_validation.periods` 和 `sections.scaling_readiness`。
- 同步 panel 超时后并发闸门阻止新的慢查询堆积。

## Migration, Rollback, Compatibility

无迁移。回滚只需撤销本次代码改动；外部接口和存储格式不变。

## Configuration and Environment Isolation

不新增环境变量。同步 panel 并发限制使用代码常量，测试中可 patch。

## Code Organization and Dependencies

不引入新第三方依赖。改动集中在现有 dashboard 模块和测试。

## Documentation and Operations Manual

本文件记录审查修复边界；部署仍遵循 `CLAUDE.md`：提交后使用 `bash scripts/deploy.sh --skip-commit`。

## Deployment and Acceptance Criteria

验收标准：lint 通过，相关单元/集成测试通过；页面局部刷新不再阻断整页刷新，加载更多决策不再短暂清空，Trial Review 抽屉展示真实周期，慢 panel 超时不会无限叠加后台同步查询。
