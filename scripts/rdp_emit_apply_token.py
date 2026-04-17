#!/usr/bin/env python3
"""紧急运维通道：签发 TTL-bounded apply/rollback/freeze HMAC token.

批次 A · A-0.5 交付物。正常路径应走 ``POST /rdp/operator-tokens`` API（它会
在当前 operator session 下签发 token，把身份绑进 token 载荷）。本 CLI 只用于
**API 层不可达的应急通道**（例如前端挂了、gateway 无法登录），必须由运维本人
在 shell 里执行，不得放入 crontab/CI/镜像构建。

用法:
    # Windows
    .venv\\Scripts\\python.exe scripts/rdp_emit_apply_token.py \\
        --actor <operator_name> --action apply

    # WSL2 / Linux
    python scripts/rdp_emit_apply_token.py --actor <operator_name> --action apply

前置条件:
    环境变量 ``RDP_APPLY_TOKEN_SECRET`` 必须已注入（与 API 进程共享同一 secret）。
    TTL 由 ``RDP_APPLY_TOKEN_TTL_SECONDS`` 覆盖，默认 300s，clamp 到 [60, 900]。

输出:
    到 stdout 打印 token 字符串本身（方便 ``$(...)`` 捕获），到 stderr 打印
    元信息（TTL、actor/action、curl 示例）。这样即使 shell 把 stdout 管道
    到文件，人也能从 stderr 看到上下文。

退出码:
    0 = 成功
    2 = 参数或环境错误（secret 未配置 / action 非法 / actor 含分隔符）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aats.api.rdp_apply_token import emit_token, ttl_seconds  # noqa: E402


_ALLOWED_ACTIONS = ("apply", "rollback", "freeze")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "签发 TTL-bounded apply/rollback/freeze HMAC token；"
            "应急运维用，不得在 CI/cron 里调用。"
        ),
    )
    parser.add_argument(
        "--actor",
        required=True,
        help="操作员名称，会被记入 token 载荷；不得包含 '|' 分隔符。",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=_ALLOWED_ACTIONS,
        help="token 授权的动作类型。",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not os.environ.get("RDP_APPLY_TOKEN_SECRET"):
        print(
            "error: RDP_APPLY_TOKEN_SECRET 未配置——此 CLI 必须与 API 进程共享"
            "同一 secret，请从部署环境 source 凭证后再执行。",
            file=sys.stderr,
        )
        return 2

    try:
        token = emit_token(args.actor, args.action)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"# apply-token for actor={args.actor!r} action={args.action!r} "
        f"ttl={ttl_seconds()}s",
        file=sys.stderr,
    )
    print(
        "# 使用方法: curl -H 'Cookie: <session>' "
        f"-H 'X-Rdp-Apply-Token: {token}' ...",
        file=sys.stderr,
    )
    # token 本身打到 stdout，方便 `TOKEN=$(scripts/rdp_emit_apply_token.py ...)`
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
