---
name: review
description: 代码审查：以严格的高级工程师视角检查正确性、安全性、并发、测试
---

# /review — 代码审查

## 审查方法
以严格的高级工程师视角审查。这是真金白银的交易系统，任何遗漏都可能造成资金损失。

## 必查项

### 通用
- 功能正确性
- 边界条件与异常路径
- 错误处理（是否吞掉了异常？error log 足够吗？）
- 不必要的复杂度

### 金融特有
- **Fee sign 方向**：OKX 负=费用、正=返佣，系统正=费用、负=返佣
- **Decimal 精度**：金额/价格/数量是否用了 Decimal？有没有 float 精度丢失？
- **余额/仓位一致性**：修改后 portfolio snapshot 是否正确？
- **Reservation 竞态**：并发 reserve 能否超支？

### 并发与异步
- asyncio.Lock 覆盖范围是否正确？
- NATS consumer max_ack_pending 配置是否匹配？
- 数据库 advisory lock 是否必要？
- race condition：两个 fill 同时到达会怎样？

### 持久化
- OrderState 三重持久化（Postgres 列 + JSON payload + Redis）是否同步？
- outbox 事务是否原子（单 session.commit）？
- SQLAlchemy 2.0：JSON 列用 `.as_string()` 而非 `.astext`

### 安全
- 凭证是否暴露（硬编码密码、log 中打印 token）？
- --skip-gate 等跳过安全检查的路径是否有环境保护？
- 输入验证是否充分？

### 测试
- 变更行为是否有对应测试？
- mock 是否掩盖了真实 bug？
- 边界值测试是否覆盖？

## 输出格式

### 1. 严重问题（P0 — 必须修复）
可能导致资金损失、数据不一致、系统崩溃的问题

### 2. 重要问题（P1 — 应该修复）
影响可靠性、可维护性，但不会立即造成资金损失

### 3. 次要问题（P2 — 建议修复）
代码风格、可读性、潜在优化

### 4. 修复建议
每个问题附带具体修复方案和影响范围

### 5. 合并/部署判断
明确给出：是否安全合并？是否需要先修复再部署？
