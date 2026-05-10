from __future__ import annotations

from typing import Any

from aats.bootstrap.settings import (
    AATSSettings,
    StrategyHedgeOverlayMode,
    StrategyHedgeOverlayRolloutStage,
)

_ROLLOUT_STAGE_ORDER: dict[StrategyHedgeOverlayRolloutStage, int] = {
    "replay_only": 0,
    "dry_run": 1,
    "live": 2,
}
_ROLLOUT_STAGE_LABELS: dict[StrategyHedgeOverlayRolloutStage, str] = {
    "replay_only": "回放",
    "dry_run": "dry-run",
    "live": "实盘",
}
_MODE_LABELS: dict[StrategyHedgeOverlayMode, str] = {
    "independent": "独立双书",
}


def overlay_runtime_stage(settings: AATSSettings) -> StrategyHedgeOverlayRolloutStage:
    if (
        settings.guarded_execution_dry_run
        or settings.okx_simulated_trading
        or not settings.live_submit_enabled
    ):
        return "dry_run"
    return "live"


def overlay_configured_rollout_stage(
    settings: AATSSettings,
    mode: str,
) -> StrategyHedgeOverlayRolloutStage:
    if mode == "independent":
        return settings.strategy_hedge_independent_rollout_stage
    return "replay_only"


def overlay_mode_from_execution_mode(execution_mode: str | None) -> str | None:
    normalized = str(execution_mode or "").strip().lower()
    if normalized == "protective_overlay":
        return "protective"
    if normalized == "opportunistic_overlay":
        return "opportunistic"
    if normalized in {"independent_books", "independent_long_book", "independent_short_book"}:
        return "independent"
    return None


def overlay_global_rollback_sequence() -> list[str]:
    return [
        "关闭 strategy_hedge_independent_enabled",
        "确认 strategy_hedge_overlay_enabled=false",
        "回到 directional 主策略路径并观察执行面",
    ]


def overlay_rollout_status(
    settings: AATSSettings,
    *,
    mode: str,
) -> dict[str, Any]:
    runtime_stage = overlay_runtime_stage(settings)
    if mode != "independent":
        return {
            "mode": mode,
            "runtime_stage": runtime_stage,
            "configured_rollout_stage": "replay_only",
            "runtime_allowed": False,
            "blocking_reasons": [f"{mode}_overlay_retired"],
            "summary": f"{mode} overlay 已退役，运行时不再允许启用。",
            "recommended_evidence": [],
            "rollback_sequence": overlay_global_rollback_sequence(),
        }
    configured_stage = overlay_configured_rollout_stage(settings, mode)
    blockers: list[str] = []

    if configured_stage == "replay_only":
        blockers.append(f"{mode}_overlay_rollout_replay_only")
    elif _ROLLOUT_STAGE_ORDER[runtime_stage] > _ROLLOUT_STAGE_ORDER[configured_stage]:
        blockers.append(f"{mode}_overlay_rollout_stage_blocks_{runtime_stage}_runtime")

    runtime_allowed = not blockers
    mode_label = _MODE_LABELS[mode]
    configured_stage_label = _ROLLOUT_STAGE_LABELS[configured_stage]
    runtime_stage_label = _ROLLOUT_STAGE_LABELS[runtime_stage]
    if configured_stage == "replay_only":
        summary = f"{mode_label} 当前只允许回放验证，运行时不会真正放开。"
    elif runtime_allowed and configured_stage == "live":
        summary = f"{mode_label} 已放开到实盘；仍建议先看回放和 dry-run 样本再开启。"
    elif runtime_allowed:
        summary = f"{mode_label} 当前只放开到 dry-run；非实盘运行线可以继续验证。"
    else:
        summary = f"{mode_label} 当前只放开到 {configured_stage_label}；这条{runtime_stage_label}运行线不会启用。"

    recommended_evidence = ["至少 2 组历史回放样本", "至少 1 组 dry-run 观察样本"]
    return {
        "mode": mode,
        "runtime_stage": runtime_stage,
        "configured_rollout_stage": configured_stage,
        "runtime_allowed": runtime_allowed,
        "blocking_reasons": blockers,
        "summary": summary,
        "recommended_evidence": recommended_evidence,
        "rollback_sequence": overlay_global_rollback_sequence(),
    }
