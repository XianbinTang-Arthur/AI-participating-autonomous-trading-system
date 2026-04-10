#!/usr/bin/env bash
# =============================================================================
# AATS Postgres 自动备份脚本（WSL2 dev 用）
#
# - 调用 docker exec aats-postgres pg_dump
# - 输出为 custom format（-F c），方便后续 pg_restore --clean --if-exists
# - 文件名带时间戳，便于排序
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
#   */30 * * * * cd /home/<user>/aats && source deploy/wsl2-dev/.env.wsl2 && ./deploy/wsl2-dev/scripts/backup_postgres.sh >> logs/backup.log 2>&1
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-aats-postgres}"
DB_USER="${POSTGRES_USER:-aats}"
DB_NAME="${POSTGRES_DB:-aats}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BACKUP_DIR="${PROJECT_ROOT}/backups/wsl2-postgres"

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
    local out_file="${BACKUP_DIR}/aats_${timestamp}.dump"
    local tmp_file="${out_file}.partial"

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

    mv "${tmp_file}" "${out_file}"
    local size
    size="$(du -h "${out_file}" | cut -f1)"
    log "备份完成 size=${size}"
}

prune_old() {
    log "清理 ${RETENTION_DAYS} 天前的备份"
    find "${BACKUP_DIR}" -type f -name 'aats_*.dump' -mtime "+${RETENTION_DAYS}" -print -delete || true
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
