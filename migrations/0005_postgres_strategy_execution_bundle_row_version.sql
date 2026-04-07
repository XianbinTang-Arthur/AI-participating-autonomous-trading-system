-- Stage 5：给 strategy_execution_bundles 加乐观并发版本号
--
-- 多进程拆分后，决策进程可能在生成 bundle 后立刻把 status 写一次，
-- 而执行进程在收到 fill 后又会按 derived_status 把 status 写第二次。
-- 如果两次写在 commit 之间交错，最后一次写会覆盖前一次而不感知冲突。
--
-- 引入 row_version：每次 save_execution_bundle 必须把当前 row_version
-- 作为 WHERE 条件去 CAS update，update 成功后 row_version+1。
-- 失败的写需要 caller 重读 bundle、merge 自己的修改，然后重试。
--
-- 兼容性：
-- 1) ALTER TABLE ADD COLUMN IF NOT EXISTS 是幂等的
-- 2) 新列 NOT NULL DEFAULT 1，已有行被 backfill 为 1
-- 3) Postgres ≥ 11 会用 fast-path 避免 table rewrite
-- 4) 读端如果旧版本不查 row_version，列存在但被忽略，无副作用

ALTER TABLE strategy_execution_bundles
    ADD COLUMN IF NOT EXISTS row_version BIGINT NOT NULL DEFAULT 1;

-- 给 row_version 加注释，方便 dashboard / DBA 排查
COMMENT ON COLUMN strategy_execution_bundles.row_version IS
    'Optimistic concurrency token. Bumped on every save. Stage 5 multi-process safety.';
