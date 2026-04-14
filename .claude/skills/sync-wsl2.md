---
name: sync-wsl2
description: 将 Windows 代码同步到 WSL2（不部署）
---

# /sync-wsl2 — 同步代码到 WSL2

## 前提

Windows 侧改动必须已 commit。sync 脚本使用 git pull，不会携带未提交改动。

## 执行

```bash
# 首次初始化（仅一次）
bash scripts/sync_to_wsl2.sh init

# 日常同步
bash scripts/sync_to_wsl2.sh pull

# 检查状态
bash scripts/sync_to_wsl2.sh check
```

## 常见问题

### "Your local changes would be overwritten by merge"

WSL2 侧有未提交的改动（通常因为之前误用了 rsync），需要先清理：

```bash
wsl -d Ubuntu bash -c 'cd ~/aats && git checkout -- . && git clean -fd'
```

然后重新运行 `sync_to_wsl2.sh pull`。

## 绝对禁止

- **不要用 rsync** — 会制造 git dirty state，导致后续 git pull 失败
- **不要手动 scp/cp** — 用 sync 脚本
