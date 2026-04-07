# =============================================================================
# AATS 应用镜像 — 4 进程拓扑（Stage 5d）
#
# 单一镜像 + 不同 entry / 不同 process_role 启动 4 个服务：
#   gateway   → uvicorn apps.api_gateway.main:app  (FastAPI + UI + REST)
#   market    → python -m apps.market_gateway.main
#   decision  → python -m apps.decision_engine.main
#   execution → python -m apps.execution_engine.main
#
# 设计原则：
#   - Python 3.12 slim：与 WSL2 集成测试环境一致，避开 3.13/3.14 周边轮子缺口
#   - 多阶段构建：builder 装编译依赖（gcc/libpq）做 wheel cache，runtime 只留 venv
#   - 非 root 运行：UID 1000 与 docker desktop 默认 user 一致，避免 volume 权限
#   - 包含 .[nats] 可选依赖：4 进程拓扑必须走 hybrid event bus
#
# 构建：cd deploy/wsl2-dev && docker compose --profile aats build aats-base
# 上下文：项目根 (..)，由 docker-compose.aats.yml 通过 build.context 指定
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder
# 装编译依赖、生成 venv、pip install -e .[nats]，把项目源码烤进 venv 的 .pth
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg[binary] 自带 wheel，理论不需要 libpq-dev；保留 build-essential 是为
# 防止某些可选依赖落到源码安装路径上（如未来 cython 扩展）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先拷依赖描述文件，最大化 layer 缓存命中率
COPY pyproject.toml README.md ./
COPY aats/ ./aats/
COPY apps/ ./apps/

# editable 安装 + nats extra（4 进程拓扑必须走 hybrid backend）
RUN python -m venv /opt/aats-venv \
    && /opt/aats-venv/bin/pip install --upgrade pip \
    && /opt/aats-venv/bin/pip install -e ".[nats]"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# 仅保留 venv + 项目代码 + 必需运行时库；非 root 用户启动
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/aats-venv/bin:$PATH" \
    PYTHONPATH="/app"

# 运行时仅需 ca-certificates + tini（PID 1 reaper，避免 zombie 进程）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# 1000 是 docker desktop / WSL2 默认 user uid；与宿主 mount 卷权限对齐
RUN groupadd -g 1000 aats \
    && useradd -m -u 1000 -g aats -s /bin/bash aats

# 复制 venv 与项目源码
COPY --from=builder /opt/aats-venv /opt/aats-venv
WORKDIR /app
COPY --chown=aats:aats aats/ ./aats/
COPY --chown=aats:aats apps/ ./apps/
COPY --chown=aats:aats configs/ ./configs/
COPY --chown=aats:aats migrations/ ./migrations/
COPY --chown=aats:aats scripts/ ./scripts/
COPY --chown=aats:aats pyproject.toml README.md ./

USER aats

# tini 作为 PID 1，确保 SIGTERM 能传到 python 进程触发 process_lifecycle 的
# add_signal_handler 路径，避免 docker stop 等到 10s 超时才 SIGKILL。
ENTRYPOINT ["/usr/bin/tini", "--"]

# 默认 cmd = api_gateway；其余 3 个服务在 docker-compose 里 override command
CMD ["uvicorn", "apps.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
