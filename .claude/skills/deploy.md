---
name: deploy
description: 执行 AATS 标准化生产部署流水线（含故障诊断和部署后验证）
---

# /deploy — AATS 生产部署

## 适用场景
- 部署最新代码到 WSL2 Docker Compose 环境
- 排查部署失败
- 验证部署后系统状态

## 当前仓库约定
- `.env.wsl2` 位于仓库根目录 `~/aats/.env.wsl2`
- Profile env 文件位于仓库根目录，如 `~/aats/.env.derivatives.live`
- WSL2 native checkout: `~/aats`（除非 `AATS_WSL2_PROJECT` 覆盖）
- deploy.sh 兼容旧位置 `deploy/wsl2-dev/.env.wsl2`，但新部署不要放那里

## 硬规则
- **不要声称未提交的 Windows 改动已部署** — sync 基于 git pull，不携带 working tree 变更
- **不要假设 main 分支** — `sync_to_wsl2.sh pull` 跟随当前 Windows HEAD
- **不要只检查 gateway /healthz** — 所有 profile 要求的容器必须 `running healthy`
- **不要手动执行 docker compose** — 用 deploy.sh
- **不要用 rsync 同步代码** — 会制造 dirty state

## 标准部署流程

### 1. 前置检查
```bash
git status  # 必须干净，否则 sync 不会携带未提交改动
```

### 2. 执行部署
```bash
# 代码已提交（最常见）
bash scripts/deploy.sh --skip-commit

# 带自动提交
bash scripts/deploy.sh --commit "修复描述"

# 无缓存重建（依赖变更后）
bash scripts/deploy.sh --no-cache --skip-commit

# 仅部署 WSL2 现有代码（不同步 Windows）
bash scripts/deploy.sh --skip-sync --skip-commit
```

### 3. 部署后验证
```bash
# 健康检查
wsl -d Ubuntu bash -c 'curl -sf http://127.0.0.1:8011/healthz'

# 所有容器状态
wsl -d Ubuntu bash -c 'for c in aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon; do docker inspect --format "$c {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" $c 2>/dev/null || echo "$c missing"; done'

# 确认部署的代码版本
wsl -d Ubuntu bash -c 'cd ~/aats && git log --oneline -1'
```

## Profile 对应的必要容器

| Profile | 必要容器 |
|---------|----------|
| spot / spot-live / derivatives / derivatives-live | aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon |
| derivatives-live-monolith | aats-gateway aats-rdp-daemon |

## 常见故障诊断

### 构建失败
- 检查 `pyproject.toml` 依赖
- 检查 `deploy/wsl2-dev/Dockerfile` 语法
- 使用 `--no-cache` 重试

### sync 失败 "local changes would be overwritten"
WSL2 侧有 dirty state（通常因为误用了 rsync），清理后重试：
```bash
wsl -d Ubuntu bash -c 'cd ~/aats && git checkout -- . && git clean -fd'
bash scripts/deploy.sh --skip-commit
```

### Gateway 健康但后台进程失败
```bash
wsl -d Ubuntu bash -c 'cd ~/aats/deploy/wsl2-dev && docker compose -f docker-compose.yml -f docker-compose.aats.yml -f docker-compose.aats.derivatives-live.yml logs aats-market --tail 50'
wsl -d Ubuntu bash -c 'cd ~/aats/deploy/wsl2-dev && docker compose -f docker-compose.yml -f docker-compose.aats.yml -f docker-compose.aats.derivatives-live.yml logs aats-execution --tail 50'
```

### 部署成功但运行的不是最新代码
检查两侧 HEAD 是否一致：
```bash
git rev-parse HEAD                                    # Windows
wsl -d Ubuntu bash -c 'cd ~/aats && git rev-parse HEAD'  # WSL2
```
如果不一致：是否用了 `--skip-sync`？Windows 改动是否已 commit？

### env 文件找不到
优先位置：
- `~/aats/.env.wsl2`
- `~/aats/.env.derivatives.live`

旧位置（兼容但应迁移）：
- `~/aats/deploy/wsl2-dev/.env.wsl2`

### deploy.sh 后验证修改的脚本逻辑
```bash
wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/deploy.sh
wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/sync_to_wsl2.sh
```
