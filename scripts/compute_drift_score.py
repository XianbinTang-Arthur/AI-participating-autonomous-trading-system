#!/usr/bin/env python3
"""Stage 9 — Drift score CLI。

设计文档
========
docs/task/stage_9_abort_hooks_design.md §6

用法
====
::

    # 从 offline artifact 算（默认，不依赖 runtime 起没起）
    python scripts/compute_drift_score.py --stage T1 --source offline

    # 从 live http endpoint 算（需要 gateway 起着 + 配了 /system/abort_hook/state）
    python scripts/compute_drift_score.py --stage T2 --source live --window-hours 48

    # 落盘 + 额外输出
    python scripts/compute_drift_score.py --stage T3 --output report.json --json

    # 调试（显示每个 indicator 的 raw 值与归一化档位）
    python scripts/compute_drift_score.py --stage T4 --verbose

退出码
======

- 0 → total ≤ 1（clean / minor_drift），dryrun 升阶梯 gate 通过
- 1 → 运行错误（数据缺失到无法计算 / 命令行参数错误 / 网络失败）
- 2 → total == 2（noticeable_drift），禁止升阶梯但可以继续观察
- 3 → total ∈ [3, 4]（significant / severe），禁止升阶梯，要人工复盘
- 4 → total ≥ 5（critical），禁止升阶梯，建议立即 halt

这些退出码与设计文档 §6.2 严格一致，ladder 升级脚本可以用
``if ! compute_drift_score.py --stage T1; then echo "BLOCKED"; exit 1; fi``。

MVP 说明
========
本版本实现"pure function 版"和"offline artifact 版"。"live 版"的实现依赖
Stage 9 checklist-4（AbortHookService + /system/abort_hook/state endpoint），
checklist-3 这一刀先只提供 ``--source offline`` 和 ``--source mock``。
``--source live`` 保留 CLI 接口但调用时返回 exit 1 + 友好错误。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Windows 控制台默认是 GBK，print 含中文或 unicode 箭头的字符串会抛
# UnicodeEncodeError。显式把 stdout 切到 UTF-8 避免。
# 在 Linux/macOS 上 sys.stdout 通常已经是 utf-8，reconfigure 是 no-op。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
elif isinstance(sys.stdout, io.TextIOWrapper):  # pragma: no cover
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    except Exception:
        pass

# 让 script 能 import aats.* 即使从 repo 根目录之外启动
_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.services.governance_engine.drift_score import (  # noqa: E402
    DriftInputs,
    DriftReport,
    StageTier,
    compute_drift_score,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("stage9_drift_score")

_EXIT_CLEAN = 0
_EXIT_ERROR = 1
_EXIT_NOTICEABLE = 2
_EXIT_SIGNIFICANT = 3
_EXIT_CRITICAL = 4


# ─────────────────────────────────────────────────────────────────────
# 数据源：从 artifacts 目录读取（offline 模式）
# ─────────────────────────────────────────────────────────────────────


def _safe_load_json(path: pathlib.Path) -> dict[str, Any] | None:
    """不抛：文件不存在 / 解析失败都返回 None。

    drift score 对 missing 数据是健壮的（归一化为 0 + missing 标记），
    所以缺 artifact 不会让 CLI 整个崩掉，只会让对应指标 missing。
    """
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("无法读取 %s: %s", path, exc)
        return None


def _build_inputs_from_offline(
    project_root: pathlib.Path,
    *,
    stage: StageTier,
    window_hours: int,
    baseline_path: pathlib.Path | None,
) -> DriftInputs:
    """从 artifacts/ 下已有的 JSON 构造 DriftInputs。

    能找到的指标尽量填，找不到的留 None。这保证了 dryrun 阶梯早期（很多
    artifact 还没生成）CLI 仍能跑出一个 "大量 missing + total=0" 的 report，
    operator 一眼就能看到 "哦，数据源没铺齐"。
    """
    # quality monitor（数据链路 + reconciliation）
    qm = _safe_load_json(
        project_root / "artifacts/governance/quality_monitor_summary.json"
    )

    # execution realism（执行层）
    er_dir = project_root / "artifacts/research/execution_rounds"
    latest_er_summary: dict[str, Any] | None = None
    if er_dir.exists():
        rounds = sorted(
            (d for d in er_dir.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )
        if rounds:
            latest_er_summary = _safe_load_json(
                rounds[0] / "anomaly_report.json"
            ) or _safe_load_json(rounds[0] / "execution_realism_summary.json")

    # trial guard snapshot（财务 + 执行 + 决策的某些维度）
    tg = _safe_load_json(
        project_root / "artifacts/governance/trial_guard_snapshot.json"
    )

    # portfolio snapshot（余额漂移）
    portfolio = _safe_load_json(
        project_root / "artifacts/portfolio/latest_portfolio_snapshot.json"
    )

    # baseline（比对用，MVP 只记录进 notes）
    baseline = _safe_load_json(baseline_path) if baseline_path else None

    # 提取各项 —— 所有 get 都允许失败
    def _dec(val: Any) -> Decimal | None:
        if val is None:
            return None
        try:
            return Decimal(str(val))
        except Exception:
            return None

    def _int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except Exception:
            return None

    # financial
    balance_drift_ratio: Decimal | None = None
    max_drawdown_ratio: Decimal | None = None
    fee_to_pnl_ratio: Decimal | None = None
    if portfolio is not None:
        balance_drift_ratio = _dec(portfolio.get("balance_drift_ratio"))
        max_drawdown_ratio = _dec(portfolio.get("max_drawdown_ratio"))
    if tg is not None:
        fee_to_pnl_ratio = _dec(tg.get("fee_to_notional_ratio"))

    # execution
    fill_success_ratio: Decimal | None = None
    adverse_slippage_ratio: Decimal | None = None
    if latest_er_summary is not None:
        fill_success_ratio = _dec(latest_er_summary.get("fill_success_ratio"))
        adverse_slippage_ratio = _dec(latest_er_summary.get("adverse_slippage_ratio"))
    if tg is not None:
        # trial_guard 的 high_slippage_ratio 可以兜底
        if adverse_slippage_ratio is None:
            adverse_slippage_ratio = _dec(tg.get("high_slippage_ratio"))

    # decision
    decision_cycle_cadence_ratio: Decimal | None = None
    decision_error_ratio: Decimal | None = None
    if qm is not None:
        summary = qm.get("summary") or {}
        decision_cycle_cadence_ratio = _dec(summary.get("decision_cycle_cadence_ratio"))
        decision_error_ratio = _dec(summary.get("decision_error_ratio"))

    # data link
    reconciliation_mismatch_count: int | None = None
    nats_handler_error_ratio: Decimal | None = None
    okx_rate_limit_count: int | None = None
    if qm is not None:
        summary = qm.get("summary") or {}
        reconciliation_mismatch_count = _int(summary.get("reconciliation_mismatch_count"))
        nats_handler_error_ratio = _dec(summary.get("nats_handler_error_ratio"))
        okx_rate_limit_count = _int(summary.get("okx_rate_limit_count"))

    extra_notes: list[str] = []
    if baseline is not None:
        extra_notes.append(
            f"baseline={baseline_path.name if baseline_path else ''}"
        )
    if qm is None and latest_er_summary is None and portfolio is None and tg is None:
        extra_notes.append(
            "offline source: artifacts 目录下找不到任何 drift 数据源，"
            "需要先跑 quality_monitor / execution_realism / trial_guard"
        )

    return DriftInputs(
        stage=stage,
        window_hours=window_hours,
        evaluated_at=datetime.now(timezone.utc),
        balance_drift_ratio=balance_drift_ratio,
        max_drawdown_ratio=max_drawdown_ratio,
        fee_to_pnl_ratio=fee_to_pnl_ratio,
        fill_success_ratio=fill_success_ratio,
        adverse_slippage_ratio=adverse_slippage_ratio,
        decision_cycle_cadence_ratio=decision_cycle_cadence_ratio,
        decision_error_ratio=decision_error_ratio,
        reconciliation_mismatch_count=reconciliation_mismatch_count,
        nats_handler_error_ratio=nats_handler_error_ratio,
        okx_rate_limit_count=okx_rate_limit_count,
        notes=extra_notes,
    )


def _build_inputs_from_mock() -> DriftInputs:
    """Mock 数据源 —— 测试 / debug 用。返回一组全 clean 的数字。"""
    return DriftInputs(
        stage="T1",
        window_hours=24,
        evaluated_at=datetime.now(timezone.utc),
        balance_drift_ratio=Decimal("0.003"),
        max_drawdown_ratio=Decimal("0.01"),
        fee_to_pnl_ratio=Decimal("0.15"),
        fill_success_ratio=Decimal("0.995"),
        adverse_slippage_ratio=Decimal("0.005"),
        decision_cycle_cadence_ratio=Decimal("0.99"),
        decision_error_ratio=Decimal("0.001"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0001"),
        okx_rate_limit_count=0,
        notes=["mock source: 全 clean 数据, 仅用于测试 CLI 走通"],
    )


# ─────────────────────────────────────────────────────────────────────
# 输出格式化
# ─────────────────────────────────────────────────────────────────────


def _format_human_readable(report: DriftReport) -> str:
    """人类可读的一屏报告（设计文档 §6.3）。"""
    lines: list[str] = []
    lines.append(
        f"Stage 9 Drift Score — {report.stage} "
        f"(nominal {report.nominal_scale_usdt} USDT)"
    )
    lines.append(
        f"Window: last {report.window_hours}h, evaluated at "
        f"{report.evaluated_at.isoformat()}"
    )
    lines.append("")

    # 每个子类一行：类名 + mean + 每个指标的 normalized
    labels = {
        "financial": "Financial",
        "execution": "Execution",
        "decision":  "Decision ",
        "data":      "Data link",
    }
    for name in ("financial", "execution", "decision", "data"):
        sub = report.subscores[name]
        indicator_cells = "  ".join(
            f"{_short(ind.name)}={ind.normalized}{'*' if ind.missing else ''}"
            for ind in sub.indicators
        )
        lines.append(
            f"{labels[name]}    {sub.value:>4.2f}  ({indicator_cells})"
        )

    lines.append("")
    lines.append(
        f"TOTAL SCORE  {report.total_score}    ── {report.state}"
    )
    lines.append("")

    if report.allow_ladder_upgrade:
        lines.append(" → Ladder upgrade: ALLOWED")
    else:
        lines.append(" → Ladder upgrade: BLOCKED")
    lines.append(f" → Abort hook action: {report.abort_hook_action}")

    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for note in report.notes:
            lines.append(f" - {note}")

    return "\n".join(lines)


def _short(name: str) -> str:
    """把长指标名缩写为表格头可读的短字符串。"""
    aliases = {
        "balance_drift_ratio":            "balance",
        "max_drawdown_ratio":             "drawdown",
        "fee_to_pnl_ratio":               "fee/pnl",
        "fill_success_ratio":             "fill",
        "adverse_slippage_ratio":         "slippage",
        "decision_cycle_cadence_ratio":   "cadence",
        "decision_error_ratio":           "error",
        "reconciliation_mismatch_count":  "mismatch",
        "nats_handler_error_ratio":       "nats_err",
        "okx_rate_limit_count":           "rate_limit",
    }
    return aliases.get(name, name)


def _format_verbose(report: DriftReport) -> str:
    """--verbose 模式：把每个 indicator 的 raw + 归一化依据都打出来。"""
    lines = [_format_human_readable(report), "", "── indicator breakdown ──"]
    for sub in report.subscores.values():
        lines.append(f"[{sub.category}] subscore = {sub.value:.4f}")
        for ind in sub.indicators:
            raw = ind.raw if ind.raw is not None else "MISSING"
            lines.append(f"  {ind.name:36}  raw={raw}  normalized={ind.normalized}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# 退出码映射
# ─────────────────────────────────────────────────────────────────────


def _exit_code_from(report: DriftReport) -> int:
    """把 DriftReport 映射成退出码。

    - 0：score ≤ 1 且没有被 missing-data 阻断
    - 1：运行错误（CLI 上层已单独 return 1）
    - 2：score = 2，或 score ≤ 1 但有 missing data 阻断升阶梯
    - 3：score ∈ [3, 4]
    - 4：score ≥ 5

    注意：missing data 场景 score 仍然是 0，但 ``allow_ladder_upgrade=False``。
    如果简单按 score 返回 0 会让 ladder 脚本误以为可以升阶梯。把 missing-data
    阻断映射到 exit 2 (noticeable)，与 "原地观察" 的含义一致。
    """
    total = report.total_score
    if total >= 5:
        return _EXIT_CRITICAL
    if total >= 3:
        return _EXIT_SIGNIFICANT
    if total == 2:
        return _EXIT_NOTICEABLE
    # total ≤ 1
    if not report.allow_ladder_upgrade:
        return _EXIT_NOTICEABLE
    return _EXIT_CLEAN


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stage 9 drift score CLI — compute drift between current "
                    "live/offline state and dryrun baseline.",
    )
    p.add_argument(
        "--stage",
        choices=("T0", "T1", "T2", "T3", "T4"),
        required=True,
        help="当前所处阶梯（决定 nominal_scale_usdt）",
    )
    p.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="滚动窗口长度，T1 建议 48，T2+ 建议 24",
    )
    p.add_argument(
        "--source",
        choices=("live", "offline", "mock"),
        default="offline",
        help="数据源：offline=读 artifacts/，live=HTTP /system/abort_hook/state"
             "（需要 checklist-4 落地），mock=测试",
    )
    p.add_argument(
        "--project-root",
        default=str(_PROJECT_ROOT),
        help="offline 模式下的 artifact 根目录（默认 repo 根）",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="baseline JSON 路径（比对用，目前仅记录到 notes）",
    )
    p.add_argument(
        "--output",
        default=None,
        help="落盘 DriftReport JSON 到这个文件（默认不落盘）",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="stdout 打印 JSON 而不是人类可读表格",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="打印每个 indicator 的 raw 值和归一化依据",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # 1. 构造输入
    try:
        if args.source == "offline":
            baseline_path = pathlib.Path(args.baseline) if args.baseline else None
            inputs = _build_inputs_from_offline(
                pathlib.Path(args.project_root),
                stage=args.stage,
                window_hours=args.window_hours,
                baseline_path=baseline_path,
            )
        elif args.source == "mock":
            inputs = _build_inputs_from_mock()
            # 允许 mock 的 stage 被 CLI 参数覆盖
            if args.stage != inputs.stage:
                inputs = DriftInputs(
                    stage=args.stage,
                    window_hours=inputs.window_hours,
                    evaluated_at=inputs.evaluated_at,
                    balance_drift_ratio=inputs.balance_drift_ratio,
                    max_drawdown_ratio=inputs.max_drawdown_ratio,
                    fee_to_pnl_ratio=inputs.fee_to_pnl_ratio,
                    fill_success_ratio=inputs.fill_success_ratio,
                    adverse_slippage_ratio=inputs.adverse_slippage_ratio,
                    decision_cycle_cadence_ratio=inputs.decision_cycle_cadence_ratio,
                    decision_error_ratio=inputs.decision_error_ratio,
                    reconciliation_mismatch_count=inputs.reconciliation_mismatch_count,
                    nats_handler_error_ratio=inputs.nats_handler_error_ratio,
                    okx_rate_limit_count=inputs.okx_rate_limit_count,
                    notes=list(inputs.notes),
                )
        else:  # live
            print(
                "ERROR: --source live 需要 Stage 9 checklist-4 的 AbortHookService "
                "+ /system/abort_hook/state endpoint，目前未落地。先用 --source offline。",
                file=sys.stderr,
            )
            return _EXIT_ERROR
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    # 2. 计算
    report = compute_drift_score(inputs)

    # 3. 输出
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    elif args.verbose:
        print(_format_verbose(report))
    else:
        print(_format_human_readable(report))

    # 4. 落盘
    if args.output:
        out_path = pathlib.Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2, default=str)

    # 5. 退出码
    return _exit_code_from(report)


if __name__ == "__main__":
    sys.exit(main())
