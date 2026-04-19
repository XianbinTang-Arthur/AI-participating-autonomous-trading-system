# Windows ↔ WSL2 项目同步工作流

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 文档定位

| 项目 | 内容 |
|---|---|
| 创建日期 | 2026-04-07 |
| 文档作用 | 把 Windows 上的代码改动 sync 到 WSL2 native checkout 的标准流程，让 docker compose / 全量 pytest / 集成测试在 ext4 上跑而不是 9P mount 上 |
| 关联脚本 | `scripts/sync_to_wsl2.sh` |
| 维护责任 | 同步流程或目标路径变化时更新本文 + memory |

---

## 1. 为什么需要这个工作流

历史上项目代码一直直接在 `/mnt/d/文件/project/AIParticipatingAutonomousTradingSystem` (Windows mount) 上跑。但 mount 路径有几个真实痛点：

| 问题 | 影响 |
|---|---|
| 9P 文件系统性能差 | docker build context 上传 10-50× 慢、pytest 跨文件 import 慢、git status 慢 |
| 中文路径 `文件` | 部分工具（少数 wheel installer / docker buildkit 旧版）对 multi-byte path 不稳 |
| Windows 与 Linux 权限模型差异 | docker 容器内非 root user 写挂载卷易遇到 permission denied |
| 文件 mtime 精度截断 | 某些 inotify-based 工具误判文件未变 |

native ext4 上 (`/home/arthur/aats`) 跑这些工作负载没有上述问题。

---

## 2. 同步脚本：`scripts/sync_to_wsl2.sh`

### 2.1 模式速查

| 命令 | 用途 | 何时使用 |
|---|---|---|
| `init`   | 在 WSL2 内 git clone 自 /mnt/d/.../ 创建 ~/aats | 首次设置或者 ~/aats 被删了 |
| `pull`   | git fetch + ff-only merge | 日常：每次 Windows commit 后想推到 WSL2 时 |
| `check`  | 双方 git HEAD + status 对比 | 不确定双方是否同步时 |
| `status` | 仅看 WSL2 端 git status + log | 想确认 WSL2 端是否干净 |
| `path`   | 输出 WSL2 端项目绝对路径 | 拼脚本时取 WSL2 端路径 |
| `shell`  | 直接跳进 WSL2 项目目录的 bash | 手动执行多步命令 |

### 2.2 默认配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AATS_WSL2_DISTRO` | `Ubuntu` | WSL distribution 名字（`wsl --list --quiet` 看） |
| `AATS_WSL2_PROJECT` | `$HOME/aats` | WSL2 内项目目标路径 |
| `AATS_WIN_PROJECT` | 自动取脚本上级目录 | Windows 端项目源路径 |

### 2.3 关键不变量

- **只同步 git committed state**：未 commit 的 working tree 修改**不会**被同步过去——这是故意的，避免半成品被跨环境拉取
- **`git fetch + git merge --ff-only`**：拒绝 non-fast-forward，避免覆盖 WSL2 端可能存在的本地修改
- **不主动 push 到任何远端**：纯本地两端同步，不污染 origin

---

## 3. 标准日常流程

### 3.1 在 Windows 上做完代码改动后想跑 docker / pytest 全套

```bash
# Windows 端：commit 改动
git add <files>
git commit -m "..."

# Sync 到 WSL2
scripts/sync_to_wsl2.sh pull

# 在 WSL2 端跑 docker compose
wsl -d Ubuntu --cd /home/arthur/aats/deploy/wsl2-dev bash -c "
  docker compose --env-file .env.wsl2 up -d
  docker compose -f docker-compose.aats.yml --env-file .env.wsl2 build
  docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d
"
```

### 3.2 想直接进 WSL2 项目目录手动操作

```bash
scripts/sync_to_wsl2.sh shell
# 现在已经在 /home/arthur/aats 内的 bash，可以直接 docker / pytest / git
```

### 3.3 不确定双方是否同步

```bash
scripts/sync_to_wsl2.sh check
# 输出双方 HEAD 和 working tree status
```

---

## 4. 与 git push/pull 工作流的关系

`scripts/sync_to_wsl2.sh` **替代**的场景：
- Windows ↔ WSL2 两端纯本地同步（无远端、无团队、单机两环境）

**不替代**的场景：
- 想把 commit 同步给团队 → 用 `git push origin main`
- 想从 GitHub 拉别人的修改 → `git pull origin main`（在 Windows 端）然后再 `scripts/sync_to_wsl2.sh pull` 推到 WSL2
- 想跨多台机器同步 → 走 GitHub 或其他 git 远端

简而言之：**本地两端走脚本，跨机器走远端**。两套机制互补不冲突。

---

## 5. 故障排查

| 症状 | 原因 | 修法 |
|---|---|---|
| `scripts/sync_to_wsl2.sh: command not found` | chmod +x 没跑 | `chmod +x scripts/sync_to_wsl2.sh` |
| `init` 报 `$HOME/aats already exists` | 之前 init 过且未删 | 跑 `pull` 而不是 `init`；或在 WSL2 内 `rm -rf ~/aats` 后重新 init |
| `pull` 报 `Not possible to fast-forward, aborting` | WSL2 端有本地修改或 diverged commit | 在 WSL2 内 `git status` 看冲突，决定是要 commit/stash 还是 reset |
| `wsl: command not found` | 不在 Windows 环境 | 脚本只能在 Windows 上跑 |
| init 卡住中文路径 | git config core.quotepath 没关 | `git config --global core.quotepath false`（一次性） |

---

## 6. 相关文件

- 脚本本身：`scripts/sync_to_wsl2.sh`
- WSL2 环境信息：参考 memory `reference_wsl2_dev_env.md`
- Stage 7 真跑 runbook：`docs/operations/stage7_wsl2_realrun_runbook.md`（这是这个 sync 脚本第一次实战的场景）

---

## Changelog

- 2026-04-07：首版。脚本与本文一并创建，第一次 init 把 `/home/arthur/aats` 同步到 commit `12ef088`。
