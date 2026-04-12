#!/bin/bash
# =============================================================================
# Postgres 初始化脚本 — 创建 AATS 4 个环境隔离数据库
#
# 本脚本由 docker-entrypoint-initdb.d 在容器首次初始化时自动执行。
# 如果 Postgres 数据卷已存在（非首次启动），此脚本不会重跑。
# 手动补建：
#   docker compose --env-file ../../.env.wsl2 exec postgres \
#     psql -U aats -c "CREATE DATABASE aats_derivatives;"
#
# 4 个环境：
#   aats_spot              — 现货模拟盘
#   aats_derivatives       — 合约模拟盘
#   aats_live_spot         — 现货实盘
#   aats_live_derivatives  — 合约实盘
#   aats_research          — RDP 研究数据平台
# =============================================================================
set -e

DATABASES="aats_spot aats_derivatives aats_live_spot aats_live_derivatives aats_research"

for db in $DATABASES; do
    echo "Creating database: $db"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        -tc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1 \
        || psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
            -c "CREATE DATABASE $db OWNER $POSTGRES_USER;"
done

echo "All AATS databases created."
