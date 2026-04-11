-- 0014_rdp_task_queue.sql
-- RDP 任务队列: gateway 写入 pending 任务, 宿主机 daemon 轮询执行。
-- 用于桥接 Docker 容器内的 UI 操作和宿主机上的 RDP 脚本执行。

CREATE TABLE IF NOT EXISTS governance.rdp_task_queue (
    id              SERIAL PRIMARY KEY,
    task_id         VARCHAR(128)  NOT NULL UNIQUE,
    workflow        VARCHAR(64)   NOT NULL,
    status          VARCHAR(32)   NOT NULL DEFAULT 'pending',
    requested_by    VARCHAR(128)  NOT NULL DEFAULT 'operator',
    requested_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    exit_code       INTEGER,
    error_message   TEXT,
    log_tail        TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_rdp_task_queue_status
    ON governance.rdp_task_queue (status, created_at ASC);
