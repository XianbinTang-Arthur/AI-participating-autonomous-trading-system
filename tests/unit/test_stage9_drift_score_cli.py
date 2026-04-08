"""Stage 9 compute_drift_score CLI 单元测试。

覆盖 `scripts/compute_drift_score.py` 里**非纯函数**的部分：
- `_build_inputs_from_offline` 从 artifact 目录读数据
- `_exit_code_from` 退出码映射（含 missing-data 阻断的特殊情况）
- `_format_human_readable` 人类可读输出的基本骨架

纯函数部分（compute_drift_score / 归一化）在 test_stage9_drift_score.py
里已经全覆盖，这里不再重复测。

import 方式：script 下的文件通过 `importlib.util` 加载（scripts 目录不是
Python 包，没有 __init__.py）。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aats.services.governance_engine.drift_score import (
    DriftInputs,
    DriftReport,
    compute_drift_score,
)

# ─────────────────────────────────────────────────────────────────────
# 加载 scripts/compute_drift_score.py 作为 module
# ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "compute_drift_score.py"


@pytest.fixture(scope="module")
def cli_module():
    spec = importlib.util.spec_from_file_location(
        "compute_drift_score_cli", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─────────────────────────────────────────────────────────────────────
# _build_inputs_from_offline
# ─────────────────────────────────────────────────────────────────────


def test_offline_inputs_from_empty_artifact_dir(tmp_path, cli_module) -> None:
    """空目录 → 所有指标 missing，notes 提示找不到数据源。"""
    inputs = cli_module._build_inputs_from_offline(
        tmp_path,
        stage="T1",
        window_hours=24,
        baseline_path=None,
    )
    assert inputs.stage == "T1"
    assert inputs.window_hours == 24
    assert inputs.balance_drift_ratio is None
    assert inputs.fill_success_ratio is None
    assert inputs.reconciliation_mismatch_count is None
    joined = " ".join(inputs.notes)
    assert "找不到任何" in joined or "offline source" in joined


def test_offline_inputs_reads_quality_monitor_summary(tmp_path, cli_module) -> None:
    """放一个 quality_monitor_summary.json → CLI 应读出 data link 指标。"""
    qm_path = tmp_path / "artifacts/governance/quality_monitor_summary.json"
    qm_path.parent.mkdir(parents=True)
    qm_path.write_text(
        json.dumps({
            "summary": {
                "decision_cycle_cadence_ratio": 0.97,
                "decision_error_ratio": 0.01,
                "reconciliation_mismatch_count": 1,
                "nats_handler_error_ratio": 0.002,
                "okx_rate_limit_count": 2,
            }
        }),
        encoding="utf-8",
    )
    inputs = cli_module._build_inputs_from_offline(
        tmp_path,
        stage="T2",
        window_hours=24,
        baseline_path=None,
    )
    assert inputs.decision_cycle_cadence_ratio == Decimal("0.97")
    assert inputs.decision_error_ratio == Decimal("0.01")
    assert inputs.reconciliation_mismatch_count == 1
    assert inputs.nats_handler_error_ratio == Decimal("0.002")
    assert inputs.okx_rate_limit_count == 2


def test_offline_inputs_reads_trial_guard_snapshot(tmp_path, cli_module) -> None:
    """放一个 trial_guard_snapshot.json → CLI 应读出 fee_to_pnl_ratio 和 slippage。"""
    tg_path = tmp_path / "artifacts/governance/trial_guard_snapshot.json"
    tg_path.parent.mkdir(parents=True)
    tg_path.write_text(
        json.dumps({
            "fee_to_notional_ratio": 0.25,
            "high_slippage_ratio": 0.015,
        }),
        encoding="utf-8",
    )
    inputs = cli_module._build_inputs_from_offline(
        tmp_path,
        stage="T1",
        window_hours=24,
        baseline_path=None,
    )
    assert inputs.fee_to_pnl_ratio == Decimal("0.25")
    # 没有 execution_round → adverse_slippage_ratio 应该兜底到 high_slippage_ratio
    assert inputs.adverse_slippage_ratio == Decimal("0.015")


def test_offline_inputs_reads_portfolio_snapshot(tmp_path, cli_module) -> None:
    ps_path = tmp_path / "artifacts/portfolio/latest_portfolio_snapshot.json"
    ps_path.parent.mkdir(parents=True)
    ps_path.write_text(
        json.dumps({
            "balance_drift_ratio": 0.008,
            "max_drawdown_ratio": 0.025,
        }),
        encoding="utf-8",
    )
    inputs = cli_module._build_inputs_from_offline(
        tmp_path,
        stage="T2",
        window_hours=24,
        baseline_path=None,
    )
    assert inputs.balance_drift_ratio == Decimal("0.008")
    assert inputs.max_drawdown_ratio == Decimal("0.025")


def test_offline_inputs_corrupt_json_is_tolerated(tmp_path, cli_module) -> None:
    """artifact 文件存在但是坏 json → 归一化为 missing，不抛。"""
    qm_path = tmp_path / "artifacts/governance/quality_monitor_summary.json"
    qm_path.parent.mkdir(parents=True)
    qm_path.write_text("{ not a json", encoding="utf-8")
    inputs = cli_module._build_inputs_from_offline(
        tmp_path,
        stage="T1",
        window_hours=24,
        baseline_path=None,
    )
    # 不抛，指标保持 None
    assert inputs.decision_cycle_cadence_ratio is None


def test_offline_inputs_baseline_note_is_added(tmp_path, cli_module) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"source": "T0_DRY"}),
        encoding="utf-8",
    )
    inputs = cli_module._build_inputs_from_offline(
        tmp_path,
        stage="T1",
        window_hours=24,
        baseline_path=baseline_path,
    )
    joined = " ".join(inputs.notes)
    assert "baseline=baseline.json" in joined


# ─────────────────────────────────────────────────────────────────────
# _exit_code_from
# ─────────────────────────────────────────────────────────────────────


def _report_with_score(
    total: int,
    *,
    allow_upgrade: bool = True,
) -> DriftReport:
    """手工构造 DriftReport 用于 exit_code 单测（不走 compute）。"""
    return DriftReport(
        schema_version="stage9.drift_score/v1",
        evaluated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        stage="T1",
        nominal_scale_usdt=Decimal("1"),
        window_hours=24,
        subscores={},
        total_score=total,
        state="clean",
        allow_ladder_upgrade=allow_upgrade,
        abort_hook_action="none",
        notes=[],
    )


def test_exit_code_clean(cli_module) -> None:
    report = _report_with_score(0)
    assert cli_module._exit_code_from(report) == 0


def test_exit_code_minor_drift(cli_module) -> None:
    report = _report_with_score(1)
    assert cli_module._exit_code_from(report) == 0


def test_exit_code_noticeable(cli_module) -> None:
    report = _report_with_score(2)
    assert cli_module._exit_code_from(report) == 2


def test_exit_code_significant_3(cli_module) -> None:
    report = _report_with_score(3)
    assert cli_module._exit_code_from(report) == 3


def test_exit_code_significant_4(cli_module) -> None:
    report = _report_with_score(4)
    assert cli_module._exit_code_from(report) == 3


def test_exit_code_critical(cli_module) -> None:
    report = _report_with_score(5)
    assert cli_module._exit_code_from(report) == 4


def test_exit_code_critical_max(cli_module) -> None:
    report = _report_with_score(8)
    assert cli_module._exit_code_from(report) == 4


def test_exit_code_missing_blocks_exit_zero(cli_module) -> None:
    """score=0 但 allow_upgrade=False（通常因为 missing data）→ 不能 exit 0。

    这是 checklist-3 实现时发现的 bug：如果简单按 score 返 0，CI 脚本会
    误以为数据源也齐了。修正是映射到 exit 2（noticeable），与 "原地观察"
    语义一致。
    """
    report = _report_with_score(0, allow_upgrade=False)
    assert cli_module._exit_code_from(report) == 2


def test_exit_code_missing_with_score_one_blocks(cli_module) -> None:
    report = _report_with_score(1, allow_upgrade=False)
    assert cli_module._exit_code_from(report) == 2


# ─────────────────────────────────────────────────────────────────────
# _format_human_readable
# ─────────────────────────────────────────────────────────────────────


def test_format_human_readable_contains_header(cli_module) -> None:
    inputs = DriftInputs(
        stage="T3",
        window_hours=48,
        evaluated_at=datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc),
        balance_drift_ratio=Decimal("0.005"),
        max_drawdown_ratio=Decimal("0.01"),
        fee_to_pnl_ratio=Decimal("0.20"),
        fill_success_ratio=Decimal("0.99"),
        adverse_slippage_ratio=Decimal("0.01"),
        decision_cycle_cadence_ratio=Decimal("0.98"),
        decision_error_ratio=Decimal("0.005"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0005"),
        okx_rate_limit_count=0,
    )
    report = compute_drift_score(inputs)
    text = cli_module._format_human_readable(report)
    assert "Stage 9 Drift Score" in text
    assert "T3" in text
    assert "100 USDT" in text  # T3 nominal
    assert "TOTAL SCORE" in text
    assert "Ladder upgrade" in text
    # 4 个 subscore 标签都出现
    assert "Financial" in text
    assert "Execution" in text
    assert "Decision" in text
    assert "Data link" in text


def test_format_human_readable_shows_missing_flag(cli_module) -> None:
    """missing 的指标应该有 * 标记。"""
    inputs = DriftInputs(
        stage="T1",
        window_hours=24,
        evaluated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    report = compute_drift_score(inputs)
    text = cli_module._format_human_readable(report)
    # * 只在 missing 时出现
    assert "*" in text
    # BLOCKED 提示应出现（missing → allow=False）
    assert "BLOCKED" in text


def test_format_human_readable_clean_has_allowed_marker(cli_module) -> None:
    inputs = DriftInputs(
        stage="T1",
        window_hours=24,
        evaluated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        balance_drift_ratio=Decimal("0.001"),
        max_drawdown_ratio=Decimal("0.001"),
        fee_to_pnl_ratio=Decimal("0.10"),
        fill_success_ratio=Decimal("0.99"),
        adverse_slippage_ratio=Decimal("0.001"),
        decision_cycle_cadence_ratio=Decimal("0.99"),
        decision_error_ratio=Decimal("0.001"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0001"),
        okx_rate_limit_count=0,
    )
    report = compute_drift_score(inputs)
    text = cli_module._format_human_readable(report)
    assert "ALLOWED" in text


def test_short_name_alias_defined_for_all_indicators(cli_module) -> None:
    """_short 的 aliases 字典必须覆盖所有指标，否则输出里会出现长名 raw string。"""
    from aats.services.governance_engine.drift_score import _CATEGORY_MEMBERS
    for category_members in _CATEGORY_MEMBERS.values():
        for name in category_members:
            short = cli_module._short(name)
            # 至少要比原名短，且不是 fallback 回原名
            assert short != name, f"{name} 没有在 _short 的 aliases 里定义"
