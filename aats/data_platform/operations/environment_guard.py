"""环境隔离守卫模块.

工作包 D: 确保 RDP 操作在正确的环境中执行，防止 dev/staging/prod 交叉污染。

环境通过 RDP_ENV 环境变量区分:
  - dev      : 开发环境，不限制操作
  - staging  : 预发布环境，允许 apply 但有警告
  - prod     : 生产环境，严格限制
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VALID_ENVIRONMENTS = ("dev", "staging", "prod")
DEFAULT_ENVIRONMENT = "dev"
ENV_VAR_NAME = "RDP_ENV"
_PROFILE_ENVIRONMENTS = {
    "spot": "staging",
    "derivatives": "staging",
    "spot_live": "prod",
    "derivatives_live": "prod",
}


@dataclass(frozen=True)
class EnvironmentInfo:
    """当前环境信息."""
    name: str
    is_production: bool
    artifacts_root: str
    config_source: str


def get_current_environment() -> str:
    """Resolve RDP safety environment from the canonical managed profile.

    Explicit ``RDP_ENV`` remains supported for isolated development, but it
    must agree with ``AATS_PROFILE`` / ``AATS_ENV_TEMPLATE_PROFILE`` whenever a
    managed profile is present.  A live profile therefore resolves to ``prod``
    even when ``RDP_ENV`` was omitted, while ambiguous managed startup state is
    rejected instead of silently becoming ``dev``.
    """
    explicit_raw = os.environ.get(ENV_VAR_NAME)
    explicit = explicit_raw.lower().strip() if explicit_raw is not None else None
    if explicit is not None and explicit not in VALID_ENVIRONMENTS:
        raise ValueError(
            f"Invalid {ENV_VAR_NAME}='{explicit}', "
            f"must be one of: {VALID_ENVIRONMENTS}"
        )

    profile_values = {
        name: str(os.environ.get(name) or "").strip().lower()
        for name in ("AATS_PROFILE", "AATS_ENV_TEMPLATE_PROFILE")
        if str(os.environ.get(name) or "").strip()
    }
    unknown_profiles = {
        name: value
        for name, value in profile_values.items()
        if value not in _PROFILE_ENVIRONMENTS
    }
    if unknown_profiles:
        raise ValueError("invalid managed profile identity for RDP environment")
    distinct_profiles = set(profile_values.values())
    if len(distinct_profiles) > 1:
        raise ValueError("conflicting managed profile identities for RDP environment")

    profile = next(iter(distinct_profiles), None)
    derived = _PROFILE_ENVIRONMENTS.get(profile) if profile else None
    if explicit is not None and derived is not None and explicit != derived:
        raise ValueError("RDP_ENV conflicts with canonical managed profile")
    if explicit is not None:
        return explicit
    if derived is not None:
        return derived

    # compose_entrypoint always sets AATS_ENV_TEMPLATE_PROFILE together with
    # AATS_STARTUP_PROFILE.  Seeing only the latter means a managed bootstrap
    # was partially applied, so falling back to dev would be unsafe.
    if str(os.environ.get("AATS_STARTUP_PROFILE") or "").strip():
        raise ValueError("managed RDP environment identity is incomplete")
    return DEFAULT_ENVIRONMENT


def get_environment_info(root: Path) -> EnvironmentInfo:
    """获取完整环境信息."""
    env = get_current_environment()
    return EnvironmentInfo(
        name=env,
        is_production=(env == "prod"),
        artifacts_root=str(root / "artifacts"),
        config_source=str(root / "configs"),
    )


# ── 环境策略配置 ──────────────────────────────────────────────

ENVIRONMENT_POLICIES: dict[str, dict[str, Any]] = {
    "dev": {
        "allow_parameter_apply": True,
        "allow_parameter_rollback": True,
        "allow_workflow_execution": True,
        "require_gate_pass": False,
        "require_approval": False,
        "allow_direct_db_access": True,
        "observation_window_hours": 0,
        "description": "开发环境: 无限制",
    },
    "staging": {
        "allow_parameter_apply": True,
        "allow_parameter_rollback": True,
        "allow_workflow_execution": True,
        "require_gate_pass": True,
        "require_approval": False,
        "allow_direct_db_access": True,
        "observation_window_hours": 24,
        "description": "预发布环境: 需要 gate 通过，有观察窗口",
    },
    "prod": {
        "allow_parameter_apply": True,
        "allow_parameter_rollback": True,
        "allow_workflow_execution": True,
        "require_gate_pass": True,
        "require_approval": True,
        "allow_direct_db_access": False,
        "observation_window_hours": 72,
        "description": "生产环境: 需要审批、gate 通过、长观察窗口",
    },
}


def get_policy(env: str | None = None) -> dict[str, Any]:
    """获取指定环境的策略."""
    if env is None:
        env = get_current_environment()
    if env not in VALID_ENVIRONMENTS:
        raise ValueError(f"invalid RDP environment policy: {env!r}")
    return ENVIRONMENT_POLICIES[env]


# ── 守卫函数 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class GuardResult:
    """守卫检查结果."""
    allowed: bool
    environment: str
    operation: str
    reason: str


@dataclass(frozen=True)
class ReleaseGuardResult:
    """Release 创建守卫检查结果."""
    allowed: bool
    environment: str
    operation: str
    reason: str
    requested_observation_window_hours: int
    resolved_observation_window_hours: int
    run_gate: bool
    run_apply: bool


def guard_parameter_apply(env: str | None = None) -> GuardResult:
    """检查当前环境是否允许参数 apply."""
    if env is None:
        env = get_current_environment()
    policy = get_policy(env)

    if not policy["allow_parameter_apply"]:
        return GuardResult(
            allowed=False,
            environment=env,
            operation="parameter_apply",
            reason=f"parameter apply is not allowed in {env} environment",
        )

    warnings = []
    if policy["require_gate_pass"]:
        warnings.append("gate pass required")
    if policy["require_approval"]:
        warnings.append("operator approval required")
    if env == "prod":
        # A-0.5: 不再用 env flag 控制 prod 写开关，改由 HMAC apply-token
        # 在 API 层强制（aats.api.rdp_apply_token / rdp_routes）。
        warnings.append("valid X-Rdp-Apply-Token required at API layer")

    reason = "allowed"
    if warnings:
        reason = f"allowed with conditions: {', '.join(warnings)}"

    return GuardResult(
        allowed=True,
        environment=env,
        operation="parameter_apply",
        reason=reason,
    )


def guard_parameter_rollback(env: str | None = None) -> GuardResult:
    """检查当前环境是否允许参数 rollback."""
    if env is None:
        env = get_current_environment()
    policy = get_policy(env)

    if not policy["allow_parameter_rollback"]:
        return GuardResult(
            allowed=False,
            environment=env,
            operation="parameter_rollback",
            reason=f"parameter rollback is not allowed in {env} environment",
        )

    return GuardResult(
        allowed=True,
        environment=env,
        operation="parameter_rollback",
        reason="allowed",
    )


def guard_workflow_execution(
    workflow_name: str,
    env: str | None = None,
) -> GuardResult:
    """检查当前环境是否允许执行 workflow."""
    if env is None:
        env = get_current_environment()
    policy = get_policy(env)

    if not policy["allow_workflow_execution"]:
        return GuardResult(
            allowed=False,
            environment=env,
            operation=f"workflow:{workflow_name}",
            reason=f"workflow execution is not allowed in {env} environment",
        )

    return GuardResult(
        allowed=True,
        environment=env,
        operation=f"workflow:{workflow_name}",
        reason="allowed",
    )


def guard_direct_db_access(env: str | None = None) -> GuardResult:
    """检查当前环境是否允许直接数据库访问."""
    if env is None:
        env = get_current_environment()
    policy = get_policy(env)

    if not policy["allow_direct_db_access"]:
        return GuardResult(
            allowed=False,
            environment=env,
            operation="direct_db_access",
            reason=f"direct database access is not allowed in {env} (use API instead)",
        )

    return GuardResult(
        allowed=True,
        environment=env,
        operation="direct_db_access",
        reason="allowed",
    )


def get_observation_window_hours(env: str | None = None) -> int:
    """获取当前环境的观察窗口时长（小时）."""
    if env is None:
        env = get_current_environment()
    policy = get_policy(env)
    return policy.get("observation_window_hours", 72)


def guard_release_creation(
    *,
    env: str | None = None,
    run_gate: bool,
    run_apply: bool,
    observation_window_hours: int | None = None,
) -> ReleaseGuardResult:
    """检查 release 创建是否满足当前环境策略."""
    if env is None:
        env = get_current_environment()

    policy = get_policy(env)
    required_window = get_observation_window_hours(env)
    requested_window = (
        required_window
        if observation_window_hours is None
        else int(observation_window_hours)
    )

    if requested_window < 0:
        return ReleaseGuardResult(
            allowed=False,
            environment=env,
            operation="parameter_release",
            reason="observation_window_hours must be >= 0",
            requested_observation_window_hours=requested_window,
            resolved_observation_window_hours=required_window,
            run_gate=run_gate,
            run_apply=run_apply,
        )

    if policy["require_gate_pass"] and not run_gate:
        return ReleaseGuardResult(
            allowed=False,
            environment=env,
            operation="parameter_release",
            reason=f"{env} environment requires gate pass; skip_gate is not allowed",
            requested_observation_window_hours=requested_window,
            resolved_observation_window_hours=required_window,
            run_gate=run_gate,
            run_apply=run_apply,
        )

    if requested_window < required_window:
        return ReleaseGuardResult(
            allowed=False,
            environment=env,
            operation="parameter_release",
            reason=(
                f"{env} environment requires observation_window_hours >= "
                f"{required_window}"
            ),
            requested_observation_window_hours=requested_window,
            resolved_observation_window_hours=required_window,
            run_gate=run_gate,
            run_apply=run_apply,
        )

    return ReleaseGuardResult(
        allowed=True,
        environment=env,
        operation="parameter_release",
        reason="allowed",
        requested_observation_window_hours=requested_window,
        resolved_observation_window_hours=max(requested_window, required_window),
        run_gate=run_gate,
        run_apply=run_apply,
    )


def print_environment_status(root: Path) -> None:
    """打印当前环境状态."""
    info = get_environment_info(root)
    policy = get_policy(info.name)

    print("RDP Environment Status")
    print(f"  Environment:    {info.name}")
    print(f"  Is Production:  {info.is_production}")
    print(f"  Artifacts Root: {info.artifacts_root}")
    print(f"  Config Source:  {info.config_source}")
    print(f"  Description:    {policy['description']}")
    print()
    print("  Policies:")
    print(f"    Parameter Apply:    {'Yes' if policy['allow_parameter_apply'] else 'No'}")
    print(f"    Parameter Rollback: {'Yes' if policy['allow_parameter_rollback'] else 'No'}")
    print(f"    Workflow Execution: {'Yes' if policy['allow_workflow_execution'] else 'No'}")
    print(f"    Require Gate Pass:  {'Yes' if policy['require_gate_pass'] else 'No'}")
    print(f"    Require Approval:   {'Yes' if policy['require_approval'] else 'No'}")
    print(f"    Direct DB Access:   {'Yes' if policy['allow_direct_db_access'] else 'No'}")
    print(f"    Observation Window: {policy['observation_window_hours']}h")
