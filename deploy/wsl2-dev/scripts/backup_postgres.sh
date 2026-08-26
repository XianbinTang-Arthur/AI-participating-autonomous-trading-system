#!/usr/bin/env bash
# =============================================================================
# AATS Postgres 自动备份脚本（WSL2 dev 用）
#
# - 调用 docker exec aats-postgres pg_dump
# - 输出为 custom format（-F c），写入后立即校验 archive TOC 与 SHA-256
# - 文件名包含目标数据库与时间戳，避免多库备份互相覆盖
# - 自动删除 N 天之前的旧备份（默认 14 天）
# - 备份到 ../../../backups/wsl2-postgres/
#
# 用法：
#   ./backup_postgres.sh                  # 一次性备份
#   ./backup_postgres.sh --check          # 只校验环境，不写入文件
#   RETENTION_DAYS=30 ./backup_postgres.sh   # 自定义保留天数
#
# 加入 cron 示例（每 30 分钟一次）：
#   crontab -e
#   */30 * * * * cd /home/<user>/aats && source .env.wsl2 && ./deploy/wsl2-dev/scripts/backup_postgres.sh >> logs/backup.log 2>&1
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-aats-postgres}"
DB_USER="${POSTGRES_USER:-aats}"
DB_NAME="${POSTGRES_DB:-aats}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BACKUP_DIR="${AATS_BACKUP_DIR:-${PROJECT_ROOT}/backups/wsl2-postgres}"

if [[ ! "${DB_NAME}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "ERROR: POSTGRES_DB 只能包含字母、数字和下划线" >&2
    exit 64
fi

mkdir -p "${BACKUP_DIR}"

log() {
    echo "[$(date '+%F %T')] $*"
}

check_env() {
    if ! command -v docker >/dev/null 2>&1; then
        log "ERROR: docker 命令不可用，请确认 WSL2 内已经能访问 docker"
        exit 1
    fi
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}\$"; then
        log "ERROR: 容器 ${CONTAINER} 未运行，请先 docker compose up -d"
        exit 1
    fi
    if ! docker exec "${CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
        log "ERROR: ${DB_NAME} 还没 ready，等几秒重试"
        exit 1
    fi
    log "环境校验通过：container=${CONTAINER} db=${DB_NAME} retention=${RETENTION_DAYS}d"
}

run_backup() {
    local timestamp
    timestamp="$(date '+%Y%m%dT%H%M%S')"
    local out_file="${BACKUP_DIR}/${DB_NAME}_${timestamp}.dump"
    local tmp_file="${out_file}.partial"
    local checksum_file="${out_file}.sha256"
    local checksum_tmp="${checksum_file}.partial"

    if [[ -e "${out_file}" || -e "${tmp_file}" || -e "${checksum_file}" ]]; then
        log "ERROR: 目标备份或临时文件已存在，拒绝覆盖"
        exit 2
    fi

    log "开始备份 → ${out_file}"
    if ! docker exec "${CONTAINER}" pg_dump \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        -F c \
        --no-owner \
        --no-acl \
        > "${tmp_file}"; then
        log "ERROR: pg_dump 失败"
        rm -f "${tmp_file}"
        exit 2
    fi

    if ! docker exec -i "${CONTAINER}" pg_restore --list < "${tmp_file}" >/dev/null; then
        log "ERROR: 备份 archive TOC 校验失败"
        rm -f "${tmp_file}"
        exit 2
    fi
    mv "${tmp_file}" "${out_file}"
    local checksum
    checksum="$(sha256sum "${out_file}" | awk '{print $1}')"
    printf '%s  %s\n' "${checksum}" "$(basename "${out_file}")" > "${checksum_tmp}"
    mv "${checksum_tmp}" "${checksum_file}"
    local size
    size="$(du -h "${out_file}" | cut -f1)"
    log "备份完成 size=${size} sha256=${checksum}"
}

prune_old() {
    log "清理 ${RETENTION_DAYS} 天前的备份"
    while IFS= read -r -d '' old_dump; do
        log "删除过期备份: ${old_dump}"
        rm -f "${old_dump}" "${old_dump}.sha256"
    done < <(
        find "${BACKUP_DIR}" -type f -name "${DB_NAME}_*.dump" \
            -mtime "+${RETENTION_DAYS}" -print0
    )
}

main() {
    case "${1:-}" in
        --check)
            check_env
            log "仅校验：OK"
            exit 0
            ;;
        "")
            check_env
            run_backup
            prune_old
            log "全部完成"
            ;;
        *)
            echo "Usage: $0 [--check]" >&2
            exit 64
            ;;
    esac
}

main "$@"
