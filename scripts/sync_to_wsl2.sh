#!/usr/bin/env bash
# =============================================================================
# Windows ↔ WSL2 项目同步脚本
#
# 用途：把 Windows 上的 AATS 项目代码同步到 WSL2 内的 native ext4 checkout，
#       让 docker compose / pytest / venv 能在原生文件系统跑（避开 /mnt/d 的
#       9P 性能 + 中文路径 + 权限模型问题）。
#
# 设计原则：
#   1. 单一脚本，多模式：init / pull / check / status / path / shell
#   2. 在 Windows 上用 git bash 跑（也兼容 WSL2 内跑），底层走 `wsl -d <distro>`
#   3. 首选 git fetch + ff-only merge（干净 + 可审计 + 不丢提交）；rsync 兜底
#   4. 默认配置写死合理值（distro=Ubuntu，目标=~/aats），允许环境变量覆盖
#   5. 不主动 push 到任何远端，纯本地两端同步
#
# 用法：
#   ./scripts/sync_to_wsl2.sh init     # 首次：在 WSL2 ~/aats clone 自 Windows
#   ./scripts/sync_to_wsl2.sh pull     # 增量：fetch + ff-only merge
#   ./scripts/sync_to_wsl2.sh check    # 对比双方 git HEAD
#   ./scripts/sync_to_wsl2.sh status   # WSL2 端 git status + log
#   ./scripts/sync_to_wsl2.sh path     # 显示 WSL2 端项目绝对路径（其他脚本可用）
#   ./scripts/sync_to_wsl2.sh shell    # 直接跳进 WSL2 项目目录的 bash
#
# 配置（环境变量）：
#   AATS_WSL2_DISTRO       默认 "Ubuntu"
#   AATS_WSL2_PROJECT      默认 "$HOME/aats"（WSL2 内绝对路径）
#   AATS_WIN_PROJECT       默认 git rev-parse --show-toplevel（Windows 源）
#
# 退出码：
#   0 = 成功
#   1 = 配置/参数错误
#   2 = WSL2 不可用 / git 操作失败
# =============================================================================

set -euo pipefail

DISTRO="${AATS_WSL2_DISTRO:-Ubuntu}"
WSL_PROJECT="${AATS_WSL2_PROJECT:-\$HOME/aats}"

# Windows 源路径：默认取当前 git toplevel；脚本本身可能从其他目录调用，
# 所以先 cd 到脚本所在目录的上级（脚本位于 scripts/）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WIN_PROJECT="$(cd "$SCRIPT_DIR/.." && pwd)"
WIN_PROJECT="${AATS_WIN_PROJECT:-$DEFAULT_WIN_PROJECT}"

# Windows 路径转 WSL 路径：
#   D:\文件\project\AATS  → /mnt/d/文件/project/AATS
#   /d/文件/project/AATS  → /mnt/d/文件/project/AATS  (git bash 已转过)
#   /mnt/d/...            → 原样
win_to_wsl_path() {
    local p="$1"
    # 已经是 /mnt/x/... 直接返回
    if [[ "$p" == /mnt/* ]]; then
        echo "$p"
        return
    fi
    # git bash 风格 /d/... → /mnt/d/...
    if [[ "$p" =~ ^/[a-zA-Z]/ ]]; then
        local drive="${p:1:1}"
        local rest="${p:2}"
        echo "/mnt/${drive,,}${rest}"
        return
    fi
    # Windows 风格 D:\... → /mnt/d/...
    if [[ "$p" =~ ^[a-zA-Z]: ]]; then
        local drive="${p:0:1}"
        local rest="${p:2}"
        # 反斜杠转正斜杠
        rest="${rest//\\//}"
        echo "/mnt/${drive,,}${rest}"
        return
    fi
    # 其他情况原样返回（让上层报错）
    echo "$p"
}

WIN_PROJECT_WSL="$(win_to_wsl_path "$WIN_PROJECT")"

# 检查 wsl 命令存在 + 目标 distro 可用
ensure_wsl_ready() {
    if ! command -v wsl >/dev/null 2>&1; then
        echo "[ERROR] 找不到 wsl 命令，本脚本需要 Windows + WSL2 环境" >&2
        exit 2
    fi
    # wsl --list 输出 UTF-16，转换后过滤
    if ! wsl --list --quiet 2>/dev/null | iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\r' | grep -qx "$DISTRO"; then
        # 不报错——某些环境 iconv 不可用，回退到直接尝试
        :
    fi
}

# 在 WSL2 内执行一行 bash 命令
wsl_run() {
    wsl -d "$DISTRO" bash -c "$1"
}

# init: 首次 clone
cmd_init() {
    ensure_wsl_ready
    echo "[sync init] Windows 源: $WIN_PROJECT"
    echo "[sync init] WSL2 源 (mount): $WIN_PROJECT_WSL"
    echo "[sync init] WSL2 目标:    $WSL_PROJECT"
    echo

    if wsl_run "test -d $WSL_PROJECT/.git"; then
        echo "[sync init] WSL2 目标已是 git repo，跳过 clone（用 'pull' 增量更新）"
        return 0
    fi
    if wsl_run "test -e $WSL_PROJECT"; then
        echo "[ERROR] $WSL_PROJECT 已存在但不是 git repo，请手动确认后删除或改名" >&2
        exit 2
    fi

    # 用 git clone 而非 cp -r，确保 .git 元信息完整
    wsl_run "git clone '$WIN_PROJECT_WSL' $WSL_PROJECT"
    echo
    echo "[sync init] 完成。WSL2 项目就绪：$WSL_PROJECT"
    echo "[sync init] 验证："
    wsl_run "cd $WSL_PROJECT && git log --oneline -3"
}

# pull: 增量同步
cmd_pull() {
    ensure_wsl_ready
    if ! wsl_run "test -d $WSL_PROJECT/.git"; then
        echo "[ERROR] $WSL_PROJECT 还不是 git repo，先跑 'init'" >&2
        exit 2
    fi
    echo "[sync pull] 从 $WIN_PROJECT_WSL 增量同步到 $WSL_PROJECT"
    # fetch + ff-only：拒绝 non-fast-forward 避免 WSL2 端有未推送的本地修改被覆盖
    wsl_run "cd $WSL_PROJECT && git fetch '$WIN_PROJECT_WSL' main && git merge --ff-only FETCH_HEAD"
    echo
    echo "[sync pull] 同步后 HEAD："
    wsl_run "cd $WSL_PROJECT && git log --oneline -3"
}

# check: 对比双方 HEAD
cmd_check() {
    ensure_wsl_ready
    echo "=== Windows ($WIN_PROJECT) ==="
    (cd "$WIN_PROJECT" && git log --oneline -3 && echo && git status --short || true)
    echo
    echo "=== WSL2 ($DISTRO:$WSL_PROJECT) ==="
    if wsl_run "test -d $WSL_PROJECT/.git"; then
        wsl_run "cd $WSL_PROJECT && git log --oneline -3 && echo && git status --short || true"
    else
        echo "(尚未 init — 跑 'sync_to_wsl2.sh init' 创建)"
    fi
}

# status: 只看 WSL2 端
cmd_status() {
    ensure_wsl_ready
    if ! wsl_run "test -d $WSL_PROJECT/.git"; then
        echo "[ERROR] $WSL_PROJECT 不存在或不是 git repo，先跑 'init'" >&2
        exit 2
    fi
    wsl_run "cd $WSL_PROJECT && git status && echo && git log --oneline -5"
}

# path: 输出 WSL2 端项目绝对路径，便于其他脚本/工具组合
cmd_path() {
    # 解析 \$HOME 这种延迟展开
    wsl_run "cd $WSL_PROJECT && pwd"
}

# shell: 直接跳进 WSL2 项目目录
cmd_shell() {
    exec wsl -d "$DISTRO" --cd "$WSL_PROJECT" bash
}

cmd_help() {
    sed -n '2,30p' "${BASH_SOURCE[0]}"
}

cmd="${1:-help}"
case "$cmd" in
    init)   cmd_init   ;;
    pull)   cmd_pull   ;;
    check)  cmd_check  ;;
    status) cmd_status ;;
    path)   cmd_path   ;;
    shell)  cmd_shell  ;;
    help|--help|-h) cmd_help ;;
    *)
        echo "未知命令: $cmd" >&2
        cmd_help
        exit 1
        ;;
esac
