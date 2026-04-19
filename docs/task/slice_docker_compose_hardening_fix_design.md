# Slice `fix(config)+feat(deploy)+chore` — docker-compose 4 进程 managed profile 死锁根因修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 状态：**设计**
> 安全网 git tag：`pre-slice-docker-compose-hardening-fix-v1`（commit `17ba1ef`）
> 前置调查：Plan E 四层 TEMP-HACK-E 补丁（本轮会话）
> 目标交付时间：2026-04-08 同日完成

---

## 1. 背景

Stage 5/6 的多进程拆分把 AATS 搬上了 `deploy/wsl2-dev/docker-compose.aats.yml` 的 4 进程
拓扑（gateway / market / decision / execution）。之后 Stage 6 Slice 6.2 加入了 managed profile
机制（`aats/bootstrap/managed_profiles.py` + `aats/bootstrap/env_profiles.py`），把 4 套
部署变体（spot / spot_live / derivatives / derivatives_live）压到单入口 `scripts/start_api.py --profile`。

**问题**：这次 managed profile 重构**没有跟进 docker-compose 4 进程拓扑**。两个维度的裂缝：

### 1.1 裂缝 A — docker-compose 不走 env_profiles 加载器

`scripts/start_api.py`（和 `scripts/run_local.py`）会在启动前调
`aats.bootstrap.env_profiles.load_profiled_dotenv_into_process(project_root, profile)`：
1. 加载 `.env.<profile>` 文件到 `os.environ`
2. **注入 `AATS_STARTUP_PROFILE` 和 `AATS_ENV_TEMPLATE_PROFILE` 两个派生变量**，让
   `AATSSettings.load_settings()` 后续能识别出走哪条 managed profile runtime_defaults 分支

docker-compose 通过 `env_file:` 直接读 `.env.derivatives` 只完成了步骤 1，**步骤 2 被跳过**。
4 个容器进程各自直接 `uvicorn` 或 `python -m apps.xxx.main`，根本没机会调 env_profiles 加载器。

后果：`AATSSettings.startup_profile=None`，`AATSSettings.env_template_profile=None`，
managed profile runtime_defaults **完全没被合并进 settings**。`.env.derivatives` 里非默认的
杠杆字段触发 `spot_cash_runtime_requires_unit_leverage` 失败。

### 1.2 裂缝 B — 模拟盘 managed profile vs 硬化 gate 的死锁

`aats/bootstrap/managed_profiles.py` 把 4 套 profile 的 `operator_session_cookie_secure` 默认值
按环境分成两派：

| Profile | environment | cookie_secure 默认 | okx_simulated_trading |
|---|---|---|---|
| `spot` | `dev` | **False**（dev UX 友好）| True |
| `spot_live` | `prod` | True | False |
| `derivatives` | `dev` | **False**（dev UX 友好）| True |
| `derivatives_live` | `prod` | True | False |

这个"dev UX 友好"的根源：dev 环境经常跑在 HTTP 上（WSL2 的 gateway 暴露 127.0.0.1:8000），
`Secure` cookie 属性在 HTTP 下浏览器不会发送，所以 web operator console 需要 non-secure cookie 才能登录。

**但** `aats/bootstrap/config.py::_exchange_runtime_hardening_kind` 只看 runtime_layering 的
`environment_capabilities.exchange_coupled`，模拟盘也算 exchange_coupled，所以 hardening gate
在 `_validate_startup_profile_settings` 里强制要 cookie_secure=True：

```python
if settings.operator_session_configured and not settings.operator_session_cookie_secure:
    raise ValueError(f"{error_prefix}_requires_secure_operator_session_cookie")
```

再加一个死锁面：`AATS_OPERATOR_SESSION_COOKIE_SECURE` 在 `MANAGED_PROFILE_DERIVED_ENV_KEYS`
set 里，`load_settings()` 会把 explicit_overrides 里的这个 key 剥掉，**env var override 根本不生效**。
只能改 strategy_profiles yaml 或者 managed_profiles 默认值。

紧跟着第二个死锁：`_validate_operator_auth_settings` 还要求 postgres + operator_auth_enabled
+ operator_session_configured 时至少有一个 enabled admin user。但新拉起的 postgres 是空的，
admin 表里没人。所以即使 cookie_secure 过了，admin user check 还是会挂。

### 1.3 级联死锁全景图

4 个校验互相咬死，最小可启动配置需要同时打 **4 层**本地补丁（Plan E）：

```
Layer 1  AATS_STARTUP_PROFILE/ENV_TEMPLATE_PROFILE       绕过 spot_cash_runtime
  ↓
Layer 2  operator_session_cookie_secure=true (yaml)     绕过 cookie_secure hardening
  ↓
Layer 3  AATS_DATABASE_URL=postgres:5432                 绕过 .env.derivatives 的 localhost
  ↓
Layer 4  AATS_OPERATOR_SESSION_SECRET=""                 绕过 admin user 校验
```

Plan E 只是 dev 解锁，不是可交付形态。根因修复必须把这 4 层死锁全部拆掉。

---

## 2. 修复范围 + 不做清单

### 2.1 做

- **工作包 A `fix(config)`**：让 `_validate_startup_profile_settings` 和
  `_validate_operator_auth_settings` 在 **dev + simulated + exchange-coupled** 组合下放行
  cookie_secure 和 admin user 两个检查，改成 WARNING 日志不抛错。加单测覆盖正反 4 种组合。
- **工作包 B `feat(deploy)`**：
  1. 新建 `scripts/compose_entrypoint.py`，在容器启动前调 `env_profiles` 加载器补齐派生变量
  2. 改 `deploy/wsl2-dev/docker-compose.aats.yml` 让 4 个服务的 `command:` 走 entrypoint shim
  3. 把 base compose 里的 dead variable `AATS_DB_DSN` 改成 `AATS_DATABASE_URL`（pydantic 字段真名）
  4. `--profile` 选择通过 compose override 控制（保持 derivatives / spot 两份 override 文件，
     但本体变得非常瘦：只保留 `env_file:` + 最小的 profile hint env var）
- **工作包 C `chore`**：清理 Plan E 遗留的 TEMP-HACK-E 补丁
  - `configs/strategy_profiles/derivatives.yaml` 头部 TEMP-HACK-E 块（带 `operator_session_cookie_secure: true`）
  - `configs/strategy_profiles/spot.yaml` 头部 TEMP-HACK-E 块
  - `deploy/wsl2-dev/docker-compose.aats.derivatives.yml` 里的 volumes mount + AATS_OPERATOR_SESSION_SECRET 空值 override
  - `deploy/wsl2-dev/docker-compose.aats.spot.yml` 对应清理
  - Grep `TEMP-HACK-E` 要返回空

### 2.2 不做

- **不引入 auto-seed bootstrap admin 功能**：这是更大的 feature，留给独立 slice。本 slice 的思路是
  "dev+simulated 允许无 admin 启动，打 warning"，用户有需要登录 operator console 时再跑
  `scripts/seed_operator_admin.py`。
- **不改 managed profile 的 dev 默认值**：保持 `cookie_secure=False`（HTTP 下 Secure cookie 无效）。
  走"dev+simulated 放行"的 validator 分支，而不是硬改默认。
- **不动 spot_live / derivatives_live 两个 live profile**：它们默认 cookie_secure=True 本来就是对的，
  prod 一定要走严格 hardening gate，validator 里不能放行。
- **不去重构 managed profile 和 docker-compose 的职责边界**：这次只做最小对齐，不做大型架构调整。

---

## 3. 详细设计

### 3.1 工作包 A：`fix(config)` 放行 dev+simulated

#### 3.1.1 新增私有 helper

在 `aats/bootstrap/config.py` 加：

```python
def _is_dev_simulated_exchange_runtime(settings: AATSSettings) -> bool:
    """dev 环境 + 模拟盘 + exchange_coupled 的组合标记，用于 hardening gate 放行。

    这条组合特指：
    - WSL2 docker-compose 4 进程真跑（observation / drill）
    - 本地 scripts/start_api.py --profile spot/derivatives（dev 迭代）

    三条都满足的时候，hardening gate 里"必须启用 secure cookie"和
    "必须有 enabled admin user"两条检查放行，改成 WARNING 日志。
    prod/live 一律不放行（live profile 的 environment=prod，自然走不到这里）。
    """
    return (
        settings.environment == "dev"
        and getattr(settings, "okx_simulated_trading", False) is True
    )
```

#### 3.1.2 `_validate_startup_profile_settings` 的 cookie_secure 检查放行

```python
# 原代码
if settings.operator_session_configured and not settings.operator_session_cookie_secure:
    raise ValueError(f"{error_prefix}_requires_secure_operator_session_cookie")

# 新代码
if settings.operator_session_configured and not settings.operator_session_cookie_secure:
    if _is_dev_simulated_exchange_runtime(settings):
        _log.warning(
            "dev_simulated_exchange_runtime_allows_insecure_cookie "
            "error_prefix=%s "
            "(HTTP dev setup; NOT suitable for prod/live, never run guarded_live here)",
            error_prefix,
        )
    else:
        raise ValueError(f"{error_prefix}_requires_secure_operator_session_cookie")
```

#### 3.1.3 `_validate_operator_auth_settings` 的 admin user 检查放行

```python
# 原代码
def _validate_operator_auth_settings(settings: AATSSettings, storage: StorageBackends) -> None:
    if settings.storage_mode != "postgres":
        return
    if not settings.operator_auth_enabled:
        return
    if not settings.operator_session_configured:
        return
    if settings.operator_write_api_key:
        return
    if enabled_admin_count(storage.operator_repo) > 0:
        return
    raise ValueError("operator_session_auth_requires_enabled_admin_user")

# 新代码：最后一条 raise 前加一个 dev+simulated 放行分支
def _validate_operator_auth_settings(settings: AATSSettings, storage: StorageBackends) -> None:
    if settings.storage_mode != "postgres":
        return
    if not settings.operator_auth_enabled:
        return
    if not settings.operator_session_configured:
        return
    if settings.operator_write_api_key:
        return
    if enabled_admin_count(storage.operator_repo) > 0:
        return
    if _is_dev_simulated_exchange_runtime(settings):
        _log.warning(
            "dev_simulated_exchange_runtime_allows_empty_admin_user "
            "(operator console login unavailable; run scripts/seed_operator_admin.py to enable)"
        )
        return
    raise ValueError("operator_session_auth_requires_enabled_admin_user")
```

#### 3.1.4 单测覆盖

新建 `tests/unit/test_bootstrap_config_dev_simulated_hardening.py`（或者追加到现有
`test_bootstrap_config.py` 如果存在）。至少覆盖 8 个组合：

| # | environment | okx_simulated_trading | cookie_secure | admin存在 | 期望 |
|---|---|---|---|---|---|
| 1 | dev | True | False | False | pass + 2 WARNING |
| 2 | dev | True | True | False | pass + 1 WARNING（admin）|
| 3 | dev | True | False | True | pass + 1 WARNING（cookie）|
| 4 | dev | True | True | True | pass 无 warning |
| 5 | prod | False | False | False | **raise** cookie_secure |
| 6 | prod | False | True | False | **raise** admin user |
| 7 | prod | False | False | True | **raise** cookie_secure |
| 8 | prod | False | True | True | pass |

另加 2 条回归边界：
- dev + okx_simulated_trading=False（比如 paper_local）+ exchange_coupled=False → helper 返回 False
- dev + okx_simulated_trading=True + exchange_coupled=False → hardening gate 直接 early return（走不到 cookie 检查），不用放行

### 3.2 工作包 B：`feat(deploy)` entrypoint shim + base compose 修正

#### 3.2.1 新建 `scripts/compose_entrypoint.py`

```python
"""docker-compose 4 进程拓扑的 entrypoint shim。

scripts/start_api.py --profile 会调 load_profiled_dotenv_into_process() 把
AATS_STARTUP_PROFILE 和 AATS_ENV_TEMPLATE_PROFILE 两个派生变量注入 os.environ，
managed profile runtime_defaults 才能被 AATSSettings.load_settings() 正确合并。

但 docker-compose 4 进程拓扑 (aats-gateway / market / decision / execution) 直接
uvicorn 或 python -m，根本没走 start_api.py 这条路。本 shim 解决这个裂缝：

    command: ["python", "scripts/compose_entrypoint.py", "python", "-m", "apps.decision_engine.main"]
    environment:
        AATS_PROFILE: derivatives

shim 做 3 件事：
1. 读 AATS_PROFILE env var 决定走哪个 managed profile
2. 调 load_profiled_dotenv_into_process() 把派生变量注入 os.environ
3. os.execvp() 切换到真正的业务命令（后续 argv）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path("/app")


def main() -> None:
    profile = os.environ.get("AATS_PROFILE")
    if not profile:
        print(
            "[compose_entrypoint] AATS_PROFILE env var not set; "
            "skipping managed profile injection",
            file=sys.stderr,
        )
    else:
        sys.path.insert(0, str(PROJECT_ROOT))
        from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process
        dotenv_path = load_profiled_dotenv_into_process(PROJECT_ROOT, profile)
        print(
            f"[compose_entrypoint] loaded managed profile='{profile}' "
            f"dotenv='{dotenv_path.name}' "
            f"startup_profile={os.environ.get('AATS_STARTUP_PROFILE')} "
            f"env_template_profile={os.environ.get('AATS_ENV_TEMPLATE_PROFILE')}",
            file=sys.stderr,
        )

    if len(sys.argv) < 2:
        print("[compose_entrypoint] usage: python compose_entrypoint.py <cmd> [args...]", file=sys.stderr)
        sys.exit(2)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
```

#### 3.2.2 base compose 改动

1. `x-aats-common-env` 里把 `AATS_DB_DSN` 改成 `AATS_DATABASE_URL`（pydantic 字段真名）
2. 4 个服务的 `command:` 数组改成 shim 包装形式，第一个元素 `python scripts/compose_entrypoint.py`
3. 每个服务的 environment 块里加一个 `AATS_PROFILE: ${AATS_PROFILE:-}` 占位，由 override 文件填值

示例：

```yaml
aats-gateway:
  ...
  command:
    - "python"
    - "scripts/compose_entrypoint.py"
    - "uvicorn"
    - "apps.api_gateway.main:app"
    - "--host"
    - "0.0.0.0"
    - "--port"
    - "8000"
```

#### 3.2.3 profile override 文件瘦身

`docker-compose.aats.derivatives.yml` 新骨架（彻底清理）：

```yaml
name: aats-dev

x-aats-derivatives-env: &aats-derivatives-env
  AATS_PROFILE: derivatives

services:
  aats-gateway:
    env_file:
      - ../../.env.derivatives
    environment:
      <<: *aats-derivatives-env

  aats-market:
    env_file:
      - ../../.env.derivatives
    environment:
      <<: *aats-derivatives-env

  aats-decision:
    env_file:
      - ../../.env.derivatives
    environment:
      <<: *aats-derivatives-env

  aats-execution:
    env_file:
      - ../../.env.derivatives
    environment:
      <<: *aats-derivatives-env
```

对比之前 Plan E 版本，减掉：
- `AATS_ENV_TEMPLATE_PROFILE` / `AATS_STARTUP_PROFILE`（shim 会从 AATS_PROFILE 派生）
- `AATS_OPERATOR_SESSION_COOKIE_SECURE`（validator 放行）
- `AATS_DATABASE_URL`（base compose 已经正确指向 postgres 服务）
- `AATS_OPERATOR_SESSION_SECRET=""`（validator 放行）
- volumes mount（strategy yaml 不再需要补丁）
- TEMP-HACK-E 注释块

spot override 同样处理。

### 3.3 工作包 C：`chore` 清理 TEMP-HACK-E

- `configs/strategy_profiles/derivatives.yaml`: 删掉顶部 TEMP-HACK-E 块（`operator_session_cookie_secure: true` 和注释）
- `configs/strategy_profiles/spot.yaml`: 同上
- `deploy/wsl2-dev/docker-compose.aats.derivatives.yml`: 已在 3.2.3 完成清理
- `deploy/wsl2-dev/docker-compose.aats.spot.yml`: 已在 3.2.3 完成清理
- grep `TEMP-HACK-E` 全仓库应返回空

---

## 4. 验证计划

### 4.1 单测（工作包 A 提交前必过）

```bash
cd ~/aats
source ~/aats-venv/bin/activate
python -m pytest tests/unit/test_bootstrap_config_dev_simulated_hardening.py -v
```

预期 10+ 条新单测全绿，原有 test_bootstrap_config 相关断言继续通过。

### 4.2 WSL2 4 进程真跑（工作包 B+C 提交前必过）

```bash
# 用 pre-slice-docker-compose-hardening-fix-v1 tag 位置的干净 .env.derivatives
# 和清理后的 compose override 跑 4 进程 force-recreate
cd ~/aats/deploy/wsl2-dev
docker compose \
    -f docker-compose.aats.yml \
    -f docker-compose.aats.derivatives.yml \
    --env-file .env.wsl2 \
    up -d --force-recreate aats-gateway aats-market aats-decision aats-execution
sleep 30
docker ps --filter name=aats- --format 'table {{.Names}}\t{{.Status}}'
# 期望 4 个都 healthy，无 crash loop
docker logs aats-gateway --tail 40 | grep -E 'dev_simulated_exchange_runtime_allows|Application startup complete'
# 期望看到 shim log + hardening relax warning + uvicorn startup complete
docker logs aats-decision --tail 20 | grep decision_cycle
# 期望看到 decision_cycle_started / completed 每 15~30 秒一条
```

### 4.3 回归网

- `grep -rn TEMP-HACK-E ~/aats/configs ~/aats/deploy` → 返回空
- `docker compose ... config` 解析 4 个服务的最终配置，确认：
  - 没有 `volumes:` 段（volumes 清空）
  - 没有 `AATS_OPERATOR_SESSION_COOKIE_SECURE` 或 `AATS_OPERATOR_SESSION_SECRET` 覆盖
  - `AATS_DATABASE_URL` 从 base compose 来，指向 `postgres:5432`
  - `AATS_PROFILE=derivatives` 从 override 来

---

## 5. Commit 计划

| # | 类型 | 作用 | 文件 |
|---|---|---|---|
| 1 | `docs(slice-docker-compose-hardening)` | 本设计文档 | `docs/task/slice_docker_compose_hardening_fix_design.md` |
| 2 | `fix(config)` | 工作包 A + 单测 | `aats/bootstrap/config.py`, `tests/unit/test_bootstrap_config_dev_simulated_hardening.py` |
| 3 | `feat(deploy)` | 工作包 B entrypoint shim + base compose | `scripts/compose_entrypoint.py`, `deploy/wsl2-dev/docker-compose.aats.yml` |
| 4 | `chore(deploy)` | 工作包 C 清理 TEMP-HACK-E | `configs/strategy_profiles/spot.yaml`, `configs/strategy_profiles/derivatives.yaml`, `deploy/wsl2-dev/docker-compose.aats.derivatives.yml`, `deploy/wsl2-dev/docker-compose.aats.spot.yml` |

每个 commit 独立工作包，独立可 revert。如果工作包 B 真跑验证挂掉，可以单独 revert B，
工作包 A 的 validator 放行已经是独立价值。

---

## 6. 不变量

| ID | 不变量 | 验证 |
|---|---|---|
| I-A1 | dev+simulated+cookie_secure=False 不再挂 hardening gate | 单测 + WSL2 真跑 |
| I-A2 | prod+live+cookie_secure=False 仍然会挂 hardening gate | 单测 |
| I-A3 | dev+simulated+empty admin 不再挂 auth validator | 单测 + WSL2 真跑 |
| I-A4 | prod+live+empty admin 仍然会挂 auth validator | 单测 |
| I-B1 | compose entrypoint shim 接管 4 个 service 启动命令后，AATSSettings.env_template_profile 被正确设置 | 真跑 log grep |
| I-B2 | base compose 从 `AATS_DB_DSN` 改成 `AATS_DATABASE_URL` 后，pydantic settings 能读到 | 真跑 4 容器 healthy |
| I-C1 | 清理 TEMP-HACK-E 后，`grep TEMP-HACK-E` 返回空 | 静态 grep |
| I-C2 | 清理后再跑 4 进程，和清理前行为一致 | 真跑对比 |

---

## 7. 回滚策略

- 发现工作包 A 单测挂 → 不推进到工作包 B，先查 validator 改动
- 发现工作包 B 真跑 4 容器 crash loop → `git revert HEAD` 回到工作包 A 结束状态
- 发现工作包 C 清理后真跑破 → `git revert HEAD` 回到工作包 B 结束状态
- 整体失败 → `git reset --hard pre-slice-docker-compose-hardening-fix-v1` + `git tag -d pre-slice-docker-compose-hardening-fix-v1`

---

## 8. 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| validator 放行分支被 prod 误触发 | 低 | 高 | managed profile live 变体 environment=prod 硬编码，走不到放行 |
| entrypoint shim 在非 managed profile 场景下 regression | 低 | 中 | shim 检测 AATS_PROFILE 未设时 skip 注入，保持向后兼容 |
| 清理 TEMP-HACK-E 后别的调用方还在读 yaml 里被删的字段 | 低 | 低 | 删的是 yaml override 补丁，不是 managed profile 默认；默认路径一直是 False 没变过 |
| base compose AATS_DB_DSN → AATS_DATABASE_URL 破坏其他 compose 文件 | 中 | 中 | 全仓库 grep 确认 `AATS_DB_DSN` 无其他引用再动手 |
