---
name: db-query
description: 在 AATS 生产数据库中执行查询
---

# /db-query — AATS 数据库查询

## 数据库连接信息

- **Postgres 容器**: `aats-postgres`
- **用户**: `admin`
- **核心数据库**: `aats`
- **衍生品实盘数据库**: `aats_live_derivatives`

## 执行查询

```bash
# 在核心数据库查询
wsl -d Ubuntu bash -c "docker exec aats-postgres psql -U admin -d aats -c \"YOUR_SQL_HERE\""

# 在衍生品实盘数据库查询
wsl -d Ubuntu bash -c "docker exec aats-postgres psql -U admin -d aats_live_derivatives -c \"YOUR_SQL_HERE\""
```

## 常用查询

### 系统状态
```sql
-- 最近的 order states
SELECT sequence_id, status, payload->>'symbol' as symbol, created_at
FROM order_states ORDER BY sequence_id DESC LIMIT 10;

-- 活跃的 obligations
SELECT client_order_id, status, reserve_currency, payload->>'reserved_amount'
FROM order_obligations WHERE status IN ('ACTIVE', 'PARTIALLY_CONSUMED');

-- 最新 portfolio snapshot
SELECT sequence_id, total_equity, snapshot_ts
FROM portfolio_snapshots ORDER BY sequence_id DESC LIMIT 1;
```

## 注意事项

- **数据库名是 `aats_live_derivatives`**（不是 `aats_derivatives_live`）
- 不要读取或显示包含密码的环境变量
- 修改 order_states 时必须同时更新 `status` 列和 `payload` JSON 中的 `status` 字段
- execution_orders 同理：`state` 列和 `raw_payload` JSON 中的 `status` 字段
