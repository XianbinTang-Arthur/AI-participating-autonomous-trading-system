#!/usr/bin/env bash
# =============================================================================
# AATS Postgres 恢复脚本（WSL2 dev 用）
#
# 危险操作！会用备份文件覆盖当前数据库内容。
# 默认会要求二次确认，加 --yes 可跳过。
#
# 用法：
#   ./restore_postgres.sh /path/to/aats_research_20260407T120000.dump --check
#   ./restore_postgres.sh latest                              # 自动用最新一份备份
#   ./restore_postgres.sh latest --yes                        # 跳过确认
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-aats-postgres}"
DB_USER="${POSTGRES_USER:-aats}"
DB_NAME="${POSTGRES_DB:-aats}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BACKUP_DIR="${AATS_BACKUP_DIR:-${PROJECT_ROOT}/backups/wsl2-postgres}"

if [[ ! "${DB_NAME}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "ERROR: POSTGRES_DB 只能包含字母、数字和下划线" >&2
    exit 64
fi

log() {
    echo "[$(date '+%F %T')] $*"
}

usage() {
    cat <<EOF
Usage: $0 <backup-file|latest> [--check|--yes]

Examples:
  $0 ${BACKUP_DIR}/${DB_NAME}_20260407T120000.dump --check
  $0 latest --check
  $0 latest --yes
EOF
    exit 64
}

resolve_backup() {
    local arg="$1"
    if [[ "${arg}" == "latest" ]]; then
        local latest
        if [[ ! -d "${BACKUP_DIR}" ]]; then
            log "ERROR: 备份目录不存在: ${BACKUP_DIR}"
            exit 2
        fi
        latest="$(find "${BACKUP_DIR}" -type f -name "${DB_NAME}_*.dump" -printf '%T@ %p\n' \
            | sort -nr | head -n1 | cut -d' ' -f2-)"
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
        echo "$(realpath "${arg}")"
    fi
}

verify_backup() {
    local backup_file="$1"
    local basename
    local recorded_checksum
    local recorded_name
    basename="$(basename "${backup_file}")"
    if [[ "${basename}" != "${DB_NAME}_"*.dump ]]; then
        log "ERROR: 备份文件名不属于目标数据库 ${DB_NAME}: ${basename}"
        exit 2
    fi
    if [[ ! -f "${backup_file}.sha256" ]]; then
        log "ERROR: 缺少 SHA-256 sidecar: ${backup_file}.sha256"
        exit 2
    fi
    read -r recorded_checksum recorded_name < "${backup_file}.sha256"
    if [[ ! "${recorded_checksum}" =~ ^[0-9a-f]{64}$ || "${recorded_name}" != "${basename}" ]]; then
        log "ERROR: SHA-256 sidecar 格式或目标文件名无效"
        exit 2
    fi
    if ! (cd "$(dirname "${backup_file}")" && sha256sum -c "${basename}.sha256" >/dev/null); then
        log "ERROR: 备份 SHA-256 校验失败"
        exit 2
    fi
    if ! docker exec -i "${CONTAINER}" pg_restore --list < "${backup_file}" >/dev/null; then
        log "ERROR: 备份 archive TOC 校验失败"
        exit 2
    fi
    log "备份校验通过: db=${DB_NAME} file=${backup_file}"
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
    if [[ $# -gt 2 || ( -n "${2:-}" && "${2:-}" != "--check" && "${2:-}" != "--yes" ) ]]; then
        usage
    fi

    local backup_file
    backup_file="$(resolve_backup "$1")"
    verify_backup "${backup_file}"

    if [[ "${2:-}" == "--check" ]]; then
        log "仅校验：OK"
        exit 0
    fi

    local skip_confirm="no"
    if [[ "${2:-}" == "--yes" ]]; then
        skip_confirm="yes"
    fi

    if [[ "${skip_confirm}" != "yes" ]]; then
        confirm "${backup_file}"
    fi

    log "开始恢复: ${backup_file}"
    docker exec -i "${CONTAINER}" pg_restore \
        -U "${DB_USER}" -d "${DB_NAME}" --clean --if-exists \
        --exit-on-error --no-owner --no-acl < "${backup_file}"
    log "恢复完成"
}

main "$@"
