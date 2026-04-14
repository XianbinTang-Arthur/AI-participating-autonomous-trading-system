---
name: feature-delivery
description: 实现功能或修复 bug 时使用：规划→设计→编码→验证→交付报告
---

# /feature-delivery — 安全交付流程

## 适用场景
实现新功能、修复 bug、或任何涉及多文件修改的任务。

## 工作流程

### 1. 设计先行（非 trivial 改动必须）

在 `docs/task/` 下创建设计文档，覆盖以下维度（按相关性选择）：

| 维度 | 关键问题 |
|------|----------|
| 业务目标与边界 | 这个改动要解决什么问题？边界在哪里？ |
| 模块职责与领域模型 | 涉及哪些模块？它们的职责划分？ |
| 输入/输出接口 | API 签名、消息格式有变化吗？ |
| 数据库变更 | 新表/索引/约束？migration 需要吗？ |
| 事务与一致性 | 多步操作是否原子？跨表更新一致吗？ |
| 并发与竞态 | asyncio.Lock？advisory lock？max_ack_pending？ |
| 安全与权限 | 凭证泄露风险？认证绕过？ |
| 错误处理与幂等性 | 重试安全吗？partial failure 如何恢复？ |
| 状态转换与生命周期 | OrderState 三重持久化同步了吗？ |
| 缓存与性能 | Redis 缓存需要更新吗？查询需要索引吗？ |
| 日志与监控 | 关键路径有 structured logging 吗？ |
| 测试策略 | 单元测试？集成测试？需要 mock 什么？ |
| 迁移与回滚兼容 | 能不停机升级吗？回滚后数据一致吗？ |
| 配置与环境隔离 | 新的环境变量？.env 模板需要更新吗？ |
| 部署与验收标准 | 部署后怎么验证功能正确？ |

### 2. 最小化正确实现
- 只改必须改的文件
- 不做无关重构
- 保持向后兼容（除非明确要求打破）
- 不要悄悄修改公开 API

### 3. 添加/更新测试
```bash
.venv\Scripts\python.exe -m pytest tests/unit/ -k "相关模块" -x -q
```

### 4. 运行验证
```bash
# lint
.venv\Scripts\python.exe -m ruff check aats/ --fix
# 单元测试
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
```

### 5. 交付报告
返回：
1. 变更文件列表
2. 行为变化说明
3. 测试运行结果
4. 剩余风险/待办

## 硬规则
- **架构不适配时暂停**：如果现有架构无法支撑需求，停下来提出重构建议，不要硬塞
- **金融正确性优先**：fee sign、余额精度、Decimal 类型、并发安全必须正确
- **OrderState 改动必须三层同步**：Postgres 列 + JSON payload + Redis 缓存
- **重大改动三步走**：备份 → 设计 → 获批准
