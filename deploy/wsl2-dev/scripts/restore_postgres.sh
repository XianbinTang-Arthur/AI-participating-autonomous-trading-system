#!/usr/bin/env bash
# =============================================================================
# AATS Postgres 恢复脚本（WSL2 dev 用）
#
# 危险操作！会用备份文件覆盖当前数据库内容。
# 默认会要求二次确认，加 --yes 可跳过。
#
# 用法：
#   ./restore_postgres.sh /path/to/aats_20260407T120000.dump
#   ./restore_postgres.sh latest                              # 自动用最新一份备份
#   ./restore_postgres.sh latest --yes                        # 跳过确认
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-aats-postgres}"
DB_USER="${POSTGRES_USER:-aats}"
DB_NAME="${POSTGRES_DB:-aats}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BACKUP_DIR="${PROJECT_ROOT}/backups/wsl2-postgres"

log() {
    echo "[$(date '+%F %T')] $*"
}

usage() {
    cat <<EOF
Usage: $0 <backup-file|latest> [--yes]

Examples:
  $0 ${BACKUP_DIR}/aats_20260407T120000.dump
  $0 latest
  $0 latest --yes
EOF
    exit 64
}

resolve_backup() {
    local arg="$1"
    if [[ "${arg}" == "latest" ]]; then
        local latest
        latest="$(find "${BACKUP_DIR}" -type f -name 'aats_*.dump' -printf '%T@ %p\n' \
            | sort -nr | head -n1 | awk '{print $2}')"
        if [[ -z "${latest}" ]]; then
            log "ERROR: 没有找到任何备份文件 in ${BACKUP_DIR}"
            exit 2
        fi
        echo "${latest}"
    else
        if [[ ! -f "${arg}" ]]; then
            log "ERROR: 文件不存在: ${arg}"
            exit 2
        fi
        echo "${arg}"
    fi
}

confirm() {
    local backup_file="$1"
    cat <<EOF

==== 危险操作确认 ====
这将用以下备份覆盖 ${DB_NAME}@${CONTAINER}：

  备份文件: ${backup_file}
  目标 DB:  ${DB_NAME}
  操作: pg_restore --clean --if-exists  (会先 DROP 现有对象再恢复)

继续吗? 输入 yes 确认：
EOF
    read -r reply
    if [[ "${reply}" != "yes" ]]; then
        log "已取消"
        exit 1
    fi
}

main() {
    if [[ $# -lt 1 ]]; then
        usage
    fi

    local backup_file
    backup_file="$(resolve_backup "$1")"

    local skip_confirm="no"
    if [[ "${2:-}" == "--yes" ]]; then
        skip_confirm="yes"
    fi

    if [[ "${skip_confirm}" != "yes" ]]; then
        confirm "${backup_file}"
    fi

    log "开始恢复: ${backup_file}"
    cat "${backup_file}" | docker exec -i "${CONTAINER}" \
        pg_restore -U "${DB_USER}" -d "${DB_NAME}" --clean --if-exists --no-owner --no-acl
    log "恢复完成"
}

main "$@"
