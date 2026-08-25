# Windows ↔ WSL2 项目同步工作流

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：现行操作说明。最后核对：2026-08-22（代码基线 `be9179e`）。只描述代码同步；发布仍以根目录 `DEPLOYMENT.md` 为准。

## 文档定位

| 项目 | 内容 |
|---|---|
| 创建日期 | 2026-04-07 |
| 文档作用 | 把 Windows 上的已提交代码同步到 WSL2 native checkout；发布仍统一走 `scripts/deploy.sh`，测试可在 ext4 checkout 上运行 |
| 关联脚本 | `scripts/sync_to_wsl2.sh` |
| 维护责任 | 同步流程或目标路径变化时同步更新本文与 `scripts/sync_to_wsl2.sh` |

---

## 1. 为什么需要这个工作流

历史上项目代码一直直接在 `/mnt/d/文件/project/AIParticipatingAutonomousTradingSystem` (Windows mount) 上跑。但 mount 路径有几个真实痛点：

| 问题 | 影响 |
|---|---|
| 9P 文件系统性能差 | docker build context 上传 10-50× 慢、pytest 跨文件 import 慢、git status 慢 |
| 中文路径 `文件` | 部分工具（少数 wheel installer / docker buildkit 旧版）对 multi-byte path 不稳 |
| Windows 与 Linux 权限模型差异 | docker 容器内非 root user 写挂载卷易遇到 permission denied |
| 文件 mtime 精度截断 | 某些 inotify-based 工具误判文件未变 |

native ext4 上（脚本默认目标为 WSL 用户的 `$HOME/aats`）运行这些工作负载，可避开上述挂载盘限制。

---

## 2. 同步脚本：`scripts/sync_to_wsl2.sh`

### 2.1 模式速查

| 命令 | 用途 | 何时使用 |
|---|---|---|
| `init`   | 从 Windows 工作区的挂载路径克隆到 WSL2 native checkout | 首次设置，或经人工确认目标路径尚不存在时 |
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

### 3.1 在 Windows 上做完代码改动后想发布或跑 WSL2 验证

```bash
# Windows 端：commit 改动
git add <files>
git commit -m "..."

# 部署衍生品模拟栈。deploy.sh 会先同步 Windows committed HEAD 到 WSL2。
# 当前审计 NO-GO 期间，标准入口拒绝所有 live profile。
bash scripts/deploy.sh --profile derivatives --skip-commit

# 如果只是想在 WSL2 native checkout 上跑测试，不发布：
scripts/sync_to_wsl2.sh pull
wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/ -x -q"
```

### 3.2 想直接进 WSL2 项目目录手动操作

```bash
scripts/sync_to_wsl2.sh shell
# 现在已经在 ~/aats 内的 bash，可以做 pytest / git / 只读排查。
# 发布仍回到 Windows repo root 使用 scripts/deploy.sh。
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
- 想把 commit 同步给团队 → 用 `git push origin <current-branch>`
- 想从 GitHub 拉别人的修改 → `git pull origin <current-branch>`（在 Windows 端）然后再 `scripts/sync_to_wsl2.sh pull` 推到 WSL2
- 想跨多台机器同步 → 走 GitHub 或其他 git 远端

简而言之：**本地两端走脚本，跨机器走远端**。两套机制互补不冲突。

---

## 5. 故障排查

| 症状 | 原因 | 修法 |
|---|---|---|
| `scripts/sync_to_wsl2.sh: command not found` | chmod +x 没跑 | `chmod +x scripts/sync_to_wsl2.sh` |
| `init` 报目标路径已存在 | 目标已是 checkout，或存在其他文件 | 若目标是项目 checkout，先运行 `status`/`check`，确认后改用 `pull`；若不是 checkout，停止并人工确认其用途，必要时先改名备份。不要直接递归删除目标目录 |
| `pull` 报 `Not possible to fast-forward, aborting` | WSL2 端有本地提交、分支漂移或源 HEAD 无法快进 | 运行 `check` 并保留现场；先确认 WSL2 独有提交是否需要建分支/备份，再由人工选择合并、变基或切换正确分支。同步脚本不会覆盖这些数据 |
| `wsl: command not found` | 不在 Windows 环境 | 脚本只能在 Windows 上跑 |
| init 卡住中文路径 | git config core.quotepath 没关 | `git config --global core.quotepath false`（一次性） |

---

## 6. 相关文件

- 脚本本身：`scripts/sync_to_wsl2.sh`
- 当前部署入口：[`DEPLOYMENT.md`](../../DEPLOYMENT.md)
- WSL2 环境与操作边界：[`deploy/wsl2-dev/README.md`](../../deploy/wsl2-dev/README.md)
- Stage 7 历史真跑记录：[`stage7_wsl2_realrun_runbook.md`](stage7_wsl2_realrun_runbook.md)（仅作历史证据，不作为当前操作手册）

---

## Changelog

- 2026-04-07：首版。脚本与本文一并创建，第一次 init 把 `/home/arthur/aats` 同步到 commit `12ef088`。
- 2026-08-22：按当前脚本复核命令、同步边界与部署入口；删除会诱导直接删除 checkout/reset 的故障处理建议，并明确历史 runbook 的非规范性。
