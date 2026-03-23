from __future__ import annotations

from aats.schemas.blocker_control import BlockerCategory


_CATEGORY_PRIORITY: dict[BlockerCategory, int] = {
    "system_execution": 0,
    "submission_mode": 100,
    "ai_decision": 200,
    "profile_control": 300,
    "external": 400,
}

_BLOCKER_PRIORITY: dict[str, int] = {
    "reconciliation_halt_required": 10,
    "derivatives_margin_buffer_auto_halt": 12,
    "derivatives_liquidation_proximity_auto_halt": 13,
    "operator_rebaseline_required": 20,
    "phase1_shadow_degraded": 25,
    "phase1_shadow_lagging": 26,
    "ai_degraded_requires_manual_review": 30,
    "account_snapshot_missing": 40,
    "account_state_stale": 45,
    "market_connection_down": 50,
    "market_data_stale": 55,
    "reconciliation_stale": 60,
    "rebaseline_in_progress": 70,
    "kill_switch_active": 90,
    "guarded_execution_dry_run": 110,
    "live_submit_disabled": 112,
    "okx_simulated_trading_required": 113,
    "local_demo_no_exchange_submission": 114,
    "real_market_paper_uses_local_paper_execution": 116,
    "real_money_live_not_supported": 118,
    "guarded_live_blocked_by_default": 120,
}


def blocker_priority(code: str, *, category: BlockerCategory) -> int:
    if code in _BLOCKER_PRIORITY:
        return _BLOCKER_PRIORITY[code]
    return _CATEGORY_PRIORITY[category] + 50_000
