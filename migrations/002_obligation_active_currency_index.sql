-- Migration 002: Fix P0-2 advisory lock 配套的部分索引
-- reserve_obligation_transactional 在 advisory lock 内查询
-- status IN ('ACTIVE', 'PARTIALLY_CONSUMED') AND reserve_currency = ?
-- 的 obligations。此部分索引避免全表扫描，保证锁内查询 O(active_count)。
CREATE INDEX IF NOT EXISTS ix_obligations_active_currency
ON order_obligations (reserve_currency)
WHERE status IN ('ACTIVE', 'PARTIALLY_CONSUMED');
