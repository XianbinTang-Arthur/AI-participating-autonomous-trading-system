#!/usr/bin/env bash
# =============================================================================
# WSL2 sudo wrapper：从凭证文件读密码、注入到 WSL 内 sudo 的 stdin
#
# 用途：让 Claude 在 Windows bash 里跑需要 sudo 的 WSL 命令时，
#       不必在 bash 命令文本里出现密码字面量，也不必每次写一遍 awk 提取逻辑。
#
# 设计原则：
#   1. 密码绝不出现在 bash command 文本、消息文本、ps、shell history 里
#   2. 密码只在一个 WSL bash 进程内部用管道流转：cred file → awk → sudo -S → cmd
#   3. 凭证文件路径写死成 /mnt/d/文件/芝麻开门/WSL_login.txt（用户的固定约定）
#   4. 凭证文件格式：第 1 行 = 用户名，第 2 行 = 密码，无其他内容
#   5. 任意 sudo 子命令都通过位置参数传入，用 printf %q 安全转义
#
# 用法：
#   ./scripts/wsl_sudo.sh <sudo args...>
#
# 示例：
#   ./scripts/wsl_sudo.sh apt-get install -y curl docker-compose-plugin
#   ./scripts/wsl_sudo.sh chown -R 10001:10001 /home/arthur/aats/deploy/wsl2-dev/jaeger/badger
#   ./scripts/wsl_sudo.sh systemctl restart docker
#
# 配置（环境变量）：
#   AATS_WSL2_DISTRO     默认 "Ubuntu"
#   AATS_CRED_FILE       默认 "/mnt/d/文件/芝麻开门/WSL_login.txt"
#                        （注意是 WSL 视角的路径，不是 Windows 视角）
#
# 退出码：透传 sudo 返回的退出码；若文件不存在或为空、密码错误，返回 sudo 的退出码。
#
# 安全提醒：
#   - 不要 echo 任何提取出来的密码内容
#   - 不要把这个脚本的 stdout/stderr 重定向到日志文件再让人看（sudo 自己不会泄漏密码，
#     但你跑的命令可能会显示「password」相关 prompt 文本——内容里不含密码）
#   - 凭证文件密码若错误，sudo 会输出 "incorrect password attempt"——直接报告即可，
#     不要自己复述任何密码字符
# =============================================================================

set -euo pipefail

DISTRO="${AATS_WSL2_DISTRO:-Ubuntu}"
CRED_FILE="${AATS_CRED_FILE:-/mnt/d/文件/芝麻开门/WSL_login.txt}"

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 <sudo args...>" >&2
    echo "Example: $0 apt-get install -y curl" >&2
    exit 1
fi

if ! command -v wsl >/dev/null 2>&1; then
    echo "[ERROR] wsl 命令不可用，本脚本只能在 Windows 上跑" >&2
    exit 2
fi

# 用 printf %q 把每个参数安全转义成 shell 字面量，拼成一行供 WSL 内 bash 重新解析
QUOTED_CMD=""
for arg in "$@"; do
    QUOTED_CMD+="$(printf '%q ' "$arg")"
done

# 通过 heredoc 把脚本送到 WSL bash stdin。
# 注意：heredoc tag 不加引号，所以 $QUOTED_CMD 和 $CRED_FILE 会被 Windows bash 展开；
# 而 awk 脚本里的 $0 / \$ 用反斜杠转义保护，让 awk 看到。
wsl -d "$DISTRO" bash <<WSLEOF
set -o pipefail
if [[ ! -f "$CRED_FILE" ]]; then
    echo "[ERROR] 凭证文件不存在: $CRED_FILE" >&2
    exit 2
fi
awk 'BEGIN{ORS=""} NR==2 {sub(/[\r\n]+\$/,""); print}' "$CRED_FILE" \
    | sudo -S -p "" $QUOTED_CMD
WSLEOF
