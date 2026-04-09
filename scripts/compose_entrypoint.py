"""docker-compose 4 进程拓扑的 managed profile entrypoint shim.

背景：``scripts/start_api.py --profile`` 在启动前调
``aats.bootstrap.env_profiles.load_profiled_dotenv_into_process()``：
1. 读 ``.env.<profile>`` 文件把里面的 KEY=VALUE 注入 ``os.environ``
2. **同时注入 ``AATS_STARTUP_PROFILE`` 和 ``AATS_ENV_TEMPLATE_PROFILE`` 两个
   派生变量**，这两个是 managed profile runtime_defaults 分支的 hint

docker-compose 4 进程拓扑 (aats-gateway / aats-market / aats-decision /
aats-execution) 直接 ``uvicorn`` 或 ``python -m apps.xxx.main``，根本没走
start_api.py。结果：

- compose 的 ``env_file: .env.derivatives`` **已经完成了步骤 1**（docker 把
  文件内容读成 env var 注入容器进程初始 environment，文件本身不会进容器）
- **但步骤 2 被跳过**：没人调 env_profiles 加载器，派生变量没被注入
- 后果：``startup_profile=None``、``env_template_profile=None``，managed
  profile runtime_defaults 完全没被合进 settings，真跑时落回 spot/cash 基线，
  立刻触发 ``spot_cash_runtime_requires_unit_leverage`` 之类的 derivatives 错误

本 shim 只负责补步骤 2，不重复步骤 1（docker-compose 已经干完了）。

    command:
      - "python"
      - "scripts/compose_entrypoint.py"
      - "python"
      - "-m"
      - "apps.decision_engine.main"
    environment:
      AATS_PROFILE: derivatives

流程：
1. 读 ``AATS_PROFILE`` env var 决定走哪个 managed profile
2. 用 ``PROFILE_STARTUP_PROFILES`` 映射表把 ``AATS_STARTUP_PROFILE`` 和
   ``AATS_ENV_TEMPLATE_PROFILE`` 两个派生变量写进 ``os.environ``
3. ``os.execvp()`` 把当前 python 进程替换成真正的业务命令（后续 argv），
   子进程继承已修改的 ``os.environ``，managed profile 已经对齐

设计文档：docs/task/slice_docker_compose_hardening_fix_design.md 工作包 B
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 镜像内 WORKDIR 是 /app (见 deploy/wsl2-dev/Dockerfile)，scripts/ 和 aats/ 都
# 在这里。PYTHONPATH=/app 也由 Dockerfile 设置，所以 `from aats.bootstrap...`
# 导入是可行的。本地跑（不在 docker 里）时，这个路径可能不存在 —— shim 只设计
# 给 compose 环境用，非 compose 场景应该走 scripts/start_api.py 正规入口。
PROJECT_ROOT = Path("/app")


def _inject_managed_profile_env(profile: str) -> None:
    """把 AATS_STARTUP_PROFILE 和 AATS_ENV_TEMPLATE_PROFILE 两个派生变量
    注入 os.environ。

    跟 ``aats.bootstrap.env_profiles.load_profiled_dotenv_into_process()`` 的
    最后两行等效，但**不读 .env 文件**（docker-compose 的 env_file: 已经把
    文件内容变成容器进程初始 environment 了，shim 跑到这里的时候 API key
    之类的变量已经在 os.environ 里）。
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from aats.bootstrap.env_profiles import PROFILE_STARTUP_PROFILES

    if profile not in PROFILE_STARTUP_PROFILES:
        valid = sorted(PROFILE_STARTUP_PROFILES)
        raise ValueError(
            f"unknown managed profile '{profile}'; valid values: {valid}"
        )

    os.environ["AATS_STARTUP_PROFILE"] = PROFILE_STARTUP_PROFILES[profile]
    os.environ["AATS_ENV_TEMPLATE_PROFILE"] = profile


def main() -> None:
    profile = os.environ.get("AATS_PROFILE")
    if profile:
        try:
            _inject_managed_profile_env(profile)
        except ValueError as exc:
            print(
                f"[compose_entrypoint] failed to inject managed profile: {exc}",
                file=sys.stderr,
            )
            sys.exit(3)
        print(
            f"[compose_entrypoint] injected managed profile='{profile}' "
            f"startup_profile={os.environ.get('AATS_STARTUP_PROFILE')} "
            f"env_template_profile={os.environ.get('AATS_ENV_TEMPLATE_PROFILE')}",
            file=sys.stderr,
        )
    else:
        print(
            "[compose_entrypoint] AATS_PROFILE env var not set; "
            "skipping managed profile injection (backward compatible)",
            file=sys.stderr,
        )

    if len(sys.argv) < 2:
        print(
            "[compose_entrypoint] usage: python compose_entrypoint.py <cmd> [args...]",
            file=sys.stderr,
        )
        sys.exit(2)

    # 用 execvp 把当前进程替换成真正的业务命令，子进程继承修改后的 os.environ。
    # 不用 subprocess + wait，避免多一层进程树 + 信号转发复杂度。
    # 端口等参数由 docker-compose 模板层通过 --env-file 读取 .env.<profile>
    # 的 AATS_API_PORT 直接插值到 command 和 ports 中，shim 不需要干预。
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
