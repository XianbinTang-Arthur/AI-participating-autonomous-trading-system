# Task 214 · Runtime Image Migrations Bundling

## Objective

修复 `deploy/wsl2-dev/Dockerfile` 的 runtime image 未复制 `migrations/` 目录问题，确保 live 启动时 `apply_current_migrations()` 能看到项目根迁移文件并写入 `schema_migrations`。

## Scope

- `deploy/wsl2-dev/Dockerfile`
- `tests/unit/test_process_lifecycle_and_entries.py`

## Out Of Scope

- 策略逻辑
- migration SQL 内容
- readiness gate
- operator/query truth 暴露逻辑
- 任何无关重构

## Acceptance Criteria

1. runtime image 在 `/app/migrations` 下包含项目根迁移文件。
2. 单元测试能防止未来再次漏复制 `migrations/`。
3. 变更不扩大到 compose、deploy 脚本或 migration runner 语义。
