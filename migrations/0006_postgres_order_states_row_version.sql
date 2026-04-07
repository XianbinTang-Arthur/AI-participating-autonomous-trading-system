-- Stage 5：给 order_states 加乐观并发版本号
--
-- 多进程拆分后，OrderState 是订单状态机的核心可变记录。
-- execution_proc 内部多个 background loop 可能并发改同一个 client_order_id 的 row：
--   - fill loop:           filled_qty / avg_price / status (PARTIAL→FILLED)
--   - command retry loop:  status (PENDING→SUBMITTED)
--   - reconciliation:      status (any→CANCELED if exchange said so)
--
-- 如果两个写之间没有版本号校验，最后一次 commit 会覆盖前面的状态。
-- 现在的 OrderStateMachine.merge 在大多数情况下能合并字段，但崩溃恢复
-- 时序、TOCTOU race 仍然可能让"老 snapshot 覆盖新 fill 派生的状态"。
--
-- 引入 row_version：每次 SQLAlchemy ORM UPDATE 自动 WHERE row_version = N
-- 并 SET row_version = N+1。失败时抛 StaleDataError，调用方应重读 row、
-- merge 自己的修改、再重试。
--
-- 兼容性：
-- 1) ALTER TABLE ADD COLUMN IF NOT EXISTS 是幂等的
-- 2) 新列 NOT NULL DEFAULT 1，已有行被 backfill 为 1
-- 3) Postgres ≥ 11 走 fast-path 避免 table rewrite
-- 4) 旧版读端不查 row_version，列存在但被忽略，无副作用
--
-- 关于 execution_fills (ExecutionFillModelV2):
-- 该表是 append-only，已经使用 INSERT ... ON CONFLICT (fill_id) DO NOTHING
-- 提供原子幂等性。重复 fill_id 不会冲突，UPDATE 路径不存在，所以无需
-- row_version。设计文档原文是把 P0 OCC 范围写成 "order_states +
-- execution_fills"，实际审查代码后发现 execution_fills 已经原子幂等，
-- 所以本 migration 只处理 order_states 一张表。
--
-- 关于 fill_events (FillEventModel):
-- 该表当前用 SELECT-then-INSERT 检查重复，存在 TOCTOU race。配套代码改造
-- 把 SELECT-then-INSERT 改成 INSERT ... ON CONFLICT (fill_id) DO NOTHING，
-- 同样不需要 row_version 列。

ALTER TABLE order_states
    ADD COLUMN IF NOT EXISTS row_version BIGINT NOT NULL DEFAULT 1;

COMMENT ON COLUMN order_states.row_version IS
    'SQLAlchemy version_id_col. Auto-bumped on every UPDATE. Stage 5 multi-process safety net.';

-- ─────────────────────────────────────────────────────────────────────
-- 回滚（手动执行，本仓库的 migration runner 不维护 down migration）
-- ─────────────────────────────────────────────────────────────────────
-- ALTER TABLE order_states DROP COLUMN IF EXISTS row_version;
