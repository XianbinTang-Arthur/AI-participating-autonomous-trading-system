-- 0015_rdp_task_queue_constraints.sql
-- 补充 rdp_task_queue 的 DB 层约束：status CHECK + one-active-per-workflow 部分唯一索引。

-- 1. status 只允许 4 种合法值
ALTER TABLE governance.rdp_task_queue
    ADD CONSTRAINT chk_rdp_task_status
    CHECK (status IN ('pending', 'running', 'done', 'failed'));

-- 2. 每个 workflow 同时最多一个活跃任务（pending/running）
CREATE UNIQUE INDEX IF NOT EXISTS ix_rdp_task_one_active_per_workflow
    ON governance.rdp_task_queue (workflow)
    WHERE status IN ('pending', 'running');
