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
#   - 多阶段构建：builder 按哈希锁安装 wheel，runtime 只留 venv
#   - 非 root 运行：UID 1000 与 docker desktop 默认 user 一致，避免 volume 权限
#   - 包含 .[nats,redis,otel] 可选依赖：
#     * nats  → 4 进程拓扑必须走 hybrid event bus
#     * redis → Stage 6 Slice 6.1 hot state store；4 进程必须共享 KV 缓存
#     * otel  → Stage 8 OpenTelemetry + Jaeger 分布式追踪
#
# 标准构建/部署：从仓库根运行 bash scripts/deploy.sh --profile derivatives --skip-commit
# 上下文：项目根 (..)，由 docker-compose.aats.yml 通过 build.context 指定
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder
# 按目标平台锁文件生成 venv，再以 no-deps editable 方式注册项目源码
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim@sha256:3ecf5ebe01fef4b6e81be34511fb40bf378ea7fd81ab215ba15b2775ef85413d AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先按 Linux/Python 3.12 锁文件安装全部第三方依赖，最大化 layer 缓存命中率。
# --require-hashes 阻止未审计制品进入镜像；--only-binary 禁止隐式源码构建。
COPY requirements/runtime-py312-linux-x86_64.lock ./requirements/runtime-py312-linux-x86_64.lock
RUN python -m venv /opt/aats-venv \
    && /opt/aats-venv/bin/pip install \
        --require-hashes \
        --only-binary=:all: \
        -r requirements/runtime-py312-linux-x86_64.lock

# 再拷项目源码；依赖已经由锁文件安装，editable 安装不得再次解析依赖，
# 也不得创建未锁定的隔离构建环境。
COPY pyproject.toml README.md ./
COPY aats/ ./aats/
COPY apps/ ./apps/
RUN /opt/aats-venv/bin/pip install --no-deps --no-build-isolation -e .


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# 仅保留 venv + 项目代码 + 必需运行时库；非 root 用户启动
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim@sha256:3ecf5ebe01fef4b6e81be34511fb40bf378ea7fd81ab215ba15b2775ef85413d AS runtime

# glibc malloc 调优：防止 Python 进程 peak 内存不归还 OS，反复触碰容器 limit。
# MALLOC_TRIM_THRESHOLD_: 堆顶空闲超 64KB 就 trim（默认 ~128KB，太懒）
# MALLOC_MMAP_THRESHOLD_: >128KB 分配走 mmap（free 后立即归还 OS）
# MALLOC_ARENA_MAX: 限制 arena 数量（Python GIL 下多 arena 纯浪费）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/aats-venv/bin:$PATH" \
    PYTHONPATH="/app" \
    MALLOC_TRIM_THRESHOLD_=65536 \
    MALLOC_MMAP_THRESHOLD_=131072 \
    MALLOC_ARENA_MAX=2

# 运行时需 ca-certificates + tini（PID 1 reaper）+ curl（gateway healthcheck 用）
# Stage 7 修复：原版只装了 ca-certificates + tini，但 docker-compose.aats.yml
# 的 gateway healthcheck 是 `["CMD", "curl", "-fs", ...]`。curl 只在 builder 段
# 装了，没复制到 runtime，会导致 healthcheck 直接 fail (curl: not found)。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
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

# Stage 7 修复：WORKDIR /app 是 Docker 自动以 root:root 创建的，COPY --chown
# 只能影响被复制的子目录。运行时 aats 用户要在 /app 下新建 logs/runtime、
# logs/info、logs/warning、logs/error、logs/debug、state/active_parameters 等
# 目录（aats/bootstrap/logging.py + active_parameters.py 的 mkdir 调用）。
# 不预 chown /app 的话第一次 mkdir 就 PermissionError 13 让容器起不来。
# 把 /app 整体 chown 给 aats，覆盖现在与未来所有 runtime-created 子目录。
#
# RDP Bug 4 修复：artifact_index.py 扫描 /app/artifacts/research/{experiments,
# calibration_batches,calibration_rounds,step2_rounds,step3_rounds,
# attribution_rounds,execution_rounds}。Image 如不预创建这些目录，
# artifact_indexer 每次都日志 "跳过不存在的目录"，auto_import_candidates
# 找不到任何 round → governance_cycle PARTIAL (1 task failed)。
# 即使 --replay-only 模式不产出这些目录，让它们以空目录存在也能让
# artifact_index 返回 "0 artifacts" 而不是 "skipped"。
RUN mkdir -p \
        /app/artifacts/research/experiments \
        /app/artifacts/research/calibration_batches \
        /app/artifacts/research/calibration_rounds \
        /app/artifacts/research/step2_rounds \
        /app/artifacts/research/step3_rounds \
        /app/artifacts/research/attribution_rounds \
        /app/artifacts/research/execution_rounds \
        /app/artifacts/governance \
        /app/artifacts/decision_rounds \
        /app/artifacts/decision_system \
        /app/artifacts/operations \
    && chown -R aats:aats /app

USER aats

# tini 作为 PID 1，确保 SIGTERM 能传到 python 进程触发 process_lifecycle 的
# add_signal_handler 路径，避免 docker stop 等到 10s 超时才 SIGKILL。
ENTRYPOINT ["/usr/bin/tini", "--"]

# 默认 cmd = api_gateway；其余 3 个服务在 docker-compose 里 override command
CMD ["uvicorn", "apps.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
