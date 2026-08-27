#!/usr/bin/env python3
"""Run a single replay experiment.

Phase 2 入口脚本：运行一次 replay，生成 decisions artifact + diagnostics + report。

Usage:
    python scripts/rdp_run_replay.py \\
        --family independent \\
        --symbol BTC-USDT-SWAP \\
        --timeframe 15m \\
        --start 2026-01-01 \\
        --end 2026-04-01 \\
        --dataset-version v1 \\
        --param min_confirm_ticks=3 \\
        --param min_safe_net_edge_bps=10

    # 覆盖成本模型（平铺写法，自动组装到 cost_config）
    python scripts/rdp_run_replay.py \\
        --family independent \\
        --symbol BTC-USDT-SWAP --timeframe 1m \\
        --start 2026-03-31 --end 2026-04-02 \\
        --param taker_fee_bps=3 \\
        --param slippage_bps=1.5

    # 覆盖 signal edge 校准参数
    python scripts/rdp_run_replay.py \\
        --family directional \\
        --symbol BTC-USDT-SWAP --timeframe 1m \\
        --start 2026-03-31 --end 2026-04-02 \\
        --param signal_edge_scale_bps=15 \\
        --param directional_trend_weight=0.8
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_run_replay")

_ARTIFACT_ROOT = pathlib.Path("artifacts/research/experiments")


def _normalize_dataset_version(value: str | None) -> str:
    normalized = (value or "v1.0").strip()
    return "v1.0" if normalized == "v1" else normalized


def _parse_params(param_strs: list[str]) -> dict[str, object]:
    """解析 --param key=value 参数。"""
    result: dict[str, object] = {}
    for s in param_strs:
        if "=" not in s:
            log.warning("Ignoring malformed param: %s", s)
            continue
        k, v = s.split("=", 1)
        # 自动类型推断
        try:
            result[k] = int(v)
        except ValueError:
            try:
                result[k] = float(v)
            except ValueError:
                result[k] = v
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single replay experiment")
    parser.add_argument("--family", required=True, choices=["independent", "directional"])
    parser.add_argument("--symbol", required=True, help="e.g. BTC-USDT-SWAP")
    parser.add_argument("--timeframe", required=True, help="e.g. 15m, 1H")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    parser.add_argument("--param", action="append", default=[], help="key=value parameter override")
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Legacy name: validate schema before replay; does not run DDL",
    )
    args = parser.parse_args()
    args.dataset_version = _normalize_dataset_version(args.dataset_version)

    # 延迟导入
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session, validate_rdp_schema
    from aats.data_platform.operations.strategy_tuning_registry import (
        get_combo_tuning_overrides,
    )
    from aats.data_platform.replay.adapters.directional_adapter import DirectionalReplayAdapter
    from aats.data_platform.replay.adapters.independent_adapter import IndependentReplayAdapter
    from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
    from aats.data_platform.replay.core.replay_result_writer import (
        write_decisions_csv,
        write_summary_json,
    )
    from aats.data_platform.replay.core.replay_runner import run_replay
    from aats.data_platform.replay.diagnostics.replay_diagnostics import compute_diagnostics
    from aats.data_platform.replay.registry.experiment_registry import (
        create_experiment,
        mark_experiment_failed,
        mark_experiment_running,
        mark_experiment_succeeded,
        upsert_experiment_summary,
    )
    from aats.data_platform.replay.reports.markdown_report_builder import build_experiment_report

    settings = get_settings()
    if args.ensure_schema:
        log.info("Validating schema contract (--ensure-schema legacy flag)...")
        validate_rdp_schema(settings)

    # 创建 adapter
    if args.family == "independent":
        adapter = IndependentReplayAdapter()
    else:
        adapter = DirectionalReplayAdapter()

    # 参数
    tuning_overrides = get_combo_tuning_overrides(
        pathlib.Path(__file__).resolve().parent.parent,
        args.family,
        args.timeframe,
    )
    param_dict = {
        **tuning_overrides,
        **_parse_params(args.param),
    }
    params = ReplayParameterOverrides.from_dict(
        param_dict,
        base=ReplayParameterOverrides.for_family(args.family),
    )

    start_ts = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_ts = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    with get_session(settings) as session:
        # 1. 注册实验
        exp_id = create_experiment(
            session,
            family=args.family,
            symbol=args.symbol,
            timeframe=args.timeframe,
            dataset_version=args.dataset_version,
            parameter_overrides=params.to_dict(),
            window_start_ts=start_ts,
            window_end_ts=end_ts,
        )
        mark_experiment_running(session, exp_id)
        session.commit()

        try:
            # 2. 运行 replay
            decisions = run_replay(
                session,
                adapter=adapter,
                symbol=args.symbol,
                timeframe=args.timeframe,
                dataset_version=args.dataset_version,
                start_ts=start_ts,
                end_ts=end_ts,
                params=params,
            )

            # 3. 产物目录
            exp_dir = _ARTIFACT_ROOT / str(exp_id)
            result_path = write_decisions_csv(decisions, exp_dir / "replay_decisions.csv")
            diag = compute_diagnostics(decisions)
            summary_path = write_summary_json(diag, exp_dir / "diagnostics.json")

            # 4. Report
            exp_info = {
                "experiment_id": str(exp_id),
                "family": args.family,
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "dataset_version": args.dataset_version,
                "parameter_overrides": params.to_dict(),
                "window_start_ts": args.start,
                "window_end_ts": args.end,
                "status": "succeeded",
            }
            report_path = build_experiment_report(
                experiment_info=exp_info,
                diagnostics=diag,
                output_path=exp_dir / "report.md",
            )

            # 5. 更新 registry
            upsert_experiment_summary(session, exp_id, summary=diag)
            mark_experiment_succeeded(
                session, exp_id,
                bar_count=len(decisions),
                result_path=str(result_path),
                summary_path=str(summary_path),
                report_path=str(report_path),
            )
            session.commit()

            log.info("=== Experiment %s completed ===", exp_id)
            log.info("  Decisions : %s", result_path)
            log.info("  Diagnostics: %s", summary_path)
            log.info("  Report    : %s", report_path)

            # 打印关键指标
            print(f"\n--- Experiment {exp_id} ---")
            print(f"Total bars     : {diag['total_bars']}")
            print(f"Opening count  : {diag['opening_count']}")
            print(f"Blocked count  : {diag['blocked_count']}")
            print(f"Selectable%    : {diag['selectable_ratio']:.2%}")
            print(f"ExecCompat%    : {diag['execution_compatible_ratio']:.2%}")
            print(f"Mean edge (bps): {diag['mean_expected_edge_bps']}")

        except Exception as exc:
            mark_experiment_failed(session, exp_id, error_message=str(exc))
            session.commit()
            log.exception("Experiment %s failed", exp_id)
            sys.exit(1)


if __name__ == "__main__":
    main()
