# AGENTS.md

> **重要**：先阅读项目根目录的 `CLAUDE.md` 获取完整操作手册。

## Working mode
Act as a careful implementation and review agent for this repository.
This is a **live trading system** handling real money — every change must be defensively coded and thoroughly tested.

## Before editing
1. 先阅读 `CLAUDE.md` 了解项目架构和操作约束。
2. 阅读相关设计文档（`docs/task/` 或 `docs/design/`）。
3. Summarize the current behavior briefly.
4. If the task is non-trivial, propose a short plan before making changes.
5. Avoid unrelated refactors.

## Implementation rules
- Prefer minimal changes. However, do not cobble together a solution just to implement a small feature. If the current architecture cannot adequately support the feature, development should be halted and recommendations for a refactor should be proposed.
- Follow the existing code style and folder structure.
- Preserve backward compatibility unless explicitly told otherwise.
- Add or update tests for behavior changes.
- Do not silently change public APIs.
- All text displayed on the front end must be written in clean UTF-8 Chinese; be sure to avoid encoding issues.
- OrderState 持久化涉及三层（Postgres 列 + JSON payload + Redis），修改时三者必须同步。
- SQLAlchemy 2.0: JSON 列用 `.as_string()`，不要用已废弃的 `.astext`。

## Validation
After making code changes, run:
1. lint: `.venv\Scripts\python.exe -m ruff check aats/ --fix`
2. unit tests: `.venv\Scripts\python.exe -m pytest tests/unit/ -x -q`
3. the narrowest integration test affected by the change（集成测试需在 WSL2 中运行）
4. The project runtime environment is: `.venv\Scripts\python.exe`（Windows）; `~/aats-venv`（WSL2）
5. The database connection settings used by the project are located in the file: `.env.derivatives.live`, on line 19

If any command fails, explain the failure clearly. Do not claim success without running the command.

## Deployment
- **唯一入口**: `bash scripts/deploy.sh --skip-commit`（代码已提交时）
- **不要手动执行** `docker compose` 命令
- **不要用 rsync** 同步代码到 WSL2
- 详见 `CLAUDE.md` 的部署章节

## Database
- Postgres 容器: `aats-postgres`，用户: `admin`
- 衍生品实盘库: `aats_live_derivatives`（注意命名顺序）
- **绝不读取或显示** `.env.wsl2` 等凭证文件内容

## Review checklist
Always check:
- correctness
- edge cases
- security（绝不暴露密钥/密码/token）
- performance regressions
- maintainability
- test coverage
- 金融正确性（fee sign、余额精度、并发安全）

## Final response format
Return:
1. what changed
2. risks / caveats
3. tests run and results
4. next steps only if necessary