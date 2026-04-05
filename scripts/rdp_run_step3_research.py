#!/usr/bin/env python3
"""Step 3 Research Orchestrator: 扩展参数扫描 + 端到端参数合并.

Phase 2 Step 3: independent 家族 6 组扩展参数 x 2 timeframe

目标:
  1. 运行 independent_15m_expanded 和 independent_1h_expanded 校准批次
     (entry/close threshold, de_risk/failed_thesis edge, timing, cost_buffer)
  2. 加载 Step 2 基线推荐 (signal_edge_scale, cost_model, confirm_ticks)
  3. 生成 Step 3 扩展推荐
  4. 合并 Step 2 + Step 3, 验证参数约束
  5. 输出完整参数候选和结论文档

执行:
  python scripts/rdp_run_step3_research.py
  python scripts/rdp_run_step3_research.py --step2-round-dir artifacts/research/step2_rounds/<round_id>

输出目录:
  artifacts/research/step3_rounds/<round_id>/
    batches/                              # 校准实验结果
    family_timeframe_summary.csv          # 汇总表
    family_timeframe_summary.json
    step3_expanded_recommendations.json   # Step 3 扩展推荐
    parameter_candidates_merged.json      # 合并后完整参数
    constraint_violations.json            # 约束校验结果
    phase2_step3_research_conclusion.md   # 结论文档
    round_manifest.json                   # 执行清单
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

# ── 日志（必须先于 Step 2 import，确保 basicConfig 生效）──
log = logging.getLogger("rdp.step3")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

# ── 导入 Step 2 公共函数 ──
# Step 2 script 的 main() 有 __name__ guard，模块级只有函数/常量定义，
# 可安全作为库导入。
_STEP2_SCRIPT = pathlib.Path(__file__).resolve().parent / "rdp_run_step2_research.py"
_spec = importlib.util.spec_from_file_location("_step2_mod", _STEP2_SCRIPT)
_step2 = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_step2)  # type: ignore[union-attr]

# 复用 Step 2 基础设施
_run_batch = _step2._run_batch
_collect_calibration_experiments = _step2._collect_calibration_experiments
_write_family_timeframe_summary_csv = _step2._write_family_timeframe_summary_csv
_write_family_timeframe_summary_json = _step2._write_family_timeframe_summary_json
_generate_single_ft_recommendations = _step2._generate_single_ft_recommendations
_write_manifest = _step2._write_manifest
_SYMBOL: str = _step2._SYMBOL

# Step 3 校准定义（引用 Step 2 中已定义的 expanded groups）
_CALIBRATION_DEFS: dict[str, dict[str, Any]] = _step2._CALIBRATION_DEFS

# ── 常量 ──
_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/step3_rounds")

# Step 3 只运行 independent 家族的扩展校准
_EXPANDED_ROUND_KEYS = ["independent_15m_expanded", "independent_1h_expanded"]

# 参数约束规则（与 ReplayParameterOverrides.__post_init__ 一致）
_CONSTRAINT_RULES: list[dict[str, Any]] = [
    {
        "name": "failed_thesis <= de_risk",
        "check": lambda c: c.get("failed_thesis_net_edge_bps", -1.0)
                           <= c.get("de_risk_net_edge_bps", 2.0),
        "params": ["failed_thesis_net_edge_bps", "de_risk_net_edge_bps"],
        "description": "failed_thesis_net_edge_bps 必须 <= de_risk_net_edge_bps",
    },
    {
        "name": "close <= entry",
        "check": lambda c: c.get("close_threshold", 0.15)
                           <= c.get("entry_threshold", 0.40),
        "params": ["close_threshold", "entry_threshold"],
        "description": "close_threshold 必须 <= entry_threshold",
    },
    {
        "name": "scale_in >= entry",
        "check": lambda c: c.get("scale_in_threshold", 0.60)
                           >= c.get("entry_threshold", 0.40),
        "params": ["scale_in_threshold", "entry_threshold"],
        "description": "scale_in_threshold 必须 >= entry_threshold",
    },
    {
        "name": "short_close <= short_entry",
        "check": lambda c: (
            # 仅当两者均存在时校验 (directional 家族专用)
            c.get("short_close_threshold") is None
            or c.get("short_entry_threshold") is None
            or c["short_close_threshold"] <= c["short_entry_threshold"]
        ),
        "params": ["short_close_threshold", "short_entry_threshold"],
        "description": "short_close_threshold 必须 <= short_entry_threshold (仅当二者均存在)",
    },
]


# =========================================================================
# 1. Calibration Round Runner (Step 3 版本)
# =========================================================================
#
# Step 2 的 _run_calibration_round 中 ensure_schema 逻辑硬编码为
# round_key == "independent_1h"，Step 3 需要自己的版本以支持
# expanded round keys。


def _run_step3_calibration_round(
    round_key: str,
    cal_def: dict[str, Any],
    batch_artifact_root: pathlib.Path,
    *,
    ensure_schema: bool = False,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """运行一个 expanded calibration round (一个 family/tf 组合的全部 batch)。"""
    family = cal_def["family"]
    timeframe = cal_def["timeframe"]
    batches = cal_def["batches"]

    log.info("")
    log.info("=" * 60)
    log.info("Calibration: %s (%s / %s), %d batches",
             round_key, family, timeframe, len(batches))
    log.info("=" * 60)

    batch_results: list[dict[str, Any]] = []
    for i, bdef in enumerate(batches):
        log.info("")
        log.info("  [Batch %d/%d] %s", i + 1, len(batches), bdef["description"])
        log.info("    File: %s", bdef["file"])

        # 仅在第一个 batch 触发 schema 迁移（与 round_key 无关）
        ensure = ensure_schema and (i == 0)
        result = _run_batch(
            bdef["file"], batch_artifact_root,
            ensure_schema=ensure, stop_on_error=stop_on_error,
        )
        result["_key"] = bdef["key"]

        if result["status"] == "succeeded":
            s = result.get("summary") or {}
            log.info("    -> SUCCEEDED (%d experiments)", s.get("succeeded", 0))
        elif result["status"] == "partial_success":
            s = result.get("summary") or {}
            log.info("    -> PARTIAL (%d ok, %d failed)",
                     s.get("succeeded", 0), s.get("failed", 0))
        else:
            log.error("    -> FAILED: %s", (result.get("error") or "")[:200])

        batch_results.append(result)
        if result["status"] == "failed" and stop_on_error:
            log.error("  --stop-on-error: aborting round %s", round_key)
            break

    n_ok = sum(1 for b in batch_results if b["status"] == "succeeded")
    n_fail = sum(1 for b in batch_results if b["status"] == "failed")

    return {
        "round_key": round_key,
        "family": family,
        "timeframe": timeframe,
        "batch_results": batch_results,
        "status": ("succeeded" if n_fail == 0
                   else ("failed" if n_ok == 0 else "partial_success")),
    }


# =========================================================================
# 2. Step 2 基线加载
# =========================================================================


def _load_step2_baseline(
    step2_round_dir: pathlib.Path | None = None,
    step2_artifact_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """加载 Step 2 的 parameter_candidates.json 作为基线。

    优先使用显式指定的 round_dir，否则自动查找最新的 step2 round。

    Returns:
        完整的 parameter_candidates 数据。未找到则返回空结构。
    """
    # 优先：显式指定的目录
    if step2_round_dir and step2_round_dir.exists():
        candidates_file = step2_round_dir / "parameter_candidates.json"
        if candidates_file.exists():
            with candidates_file.open(encoding="utf-8") as f:
                data = json.load(f)
            log.info("Loaded Step 2 baseline from %s", candidates_file)
            return data

    # 自动查找最新的 step2 round (使用脚本位置推断项目根, 避免 CWD 依赖)
    _project_root = pathlib.Path(__file__).resolve().parent.parent
    root = step2_artifact_root or (_project_root / "artifacts" / "research" / "step2_rounds")
    if root.exists():
        rounds = sorted(
            [d for d in root.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        for rd in rounds:
            cf = rd / "parameter_candidates.json"
            if cf.exists():
                with cf.open(encoding="utf-8") as f:
                    data = json.load(f)
                log.info("Auto-detected Step 2 baseline: %s", cf)
                return data

    log.warning("No Step 2 baseline found. Merge phase will use defaults.")
    return {"candidates": {}, "pending_validation": []}


# =========================================================================
# 3. 推荐合并
# =========================================================================

# Step 2 基础参数 (来自 scale / cost / ticks 校准 + 网格扫描参数)
_STEP2_BASE_PARAMS = frozenset({
    "signal_edge_scale_bps",
    "taker_fee_bps",
    "slippage_bps",
    "min_confirm_ticks",
    "min_safe_net_edge_bps",
    "score_stability_threshold",  # 网格扫描参数，需纳入合并输出
})

# Step 3 扩展参数
_STEP3_EXPANDED_PARAMS = frozenset({
    "entry_threshold",
    "close_threshold",
    "scale_in_threshold",
    "de_risk_net_edge_bps",
    "failed_thesis_net_edge_bps",
    "min_hold_seconds",
    "rebalance_cooldown_seconds",
    "expected_slippage_buffer_bps",
    "expected_execution_buffer_bps",
})

# ReplayParameterOverrides 默认值
_PARAM_DEFAULTS: dict[str, float | int] = {
    "signal_edge_scale_bps": 12.0,
    "taker_fee_bps": 5.0,
    "slippage_bps": 2.0,
    "min_confirm_ticks": 2,
    "min_safe_net_edge_bps": 0.0,
    "score_stability_threshold": 5.0,
    "entry_threshold": 0.40,
    "close_threshold": 0.15,
    "scale_in_threshold": 0.60,
    "de_risk_net_edge_bps": 2.0,
    "failed_thesis_net_edge_bps": -1.0,
    "min_hold_seconds": 300.0,
    "rebalance_cooldown_seconds": 120.0,
    "expected_slippage_buffer_bps": 0.5,
    "expected_execution_buffer_bps": 0.5,
}

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# expanded round key -> 输出用 ft_key 的映射
# 扩展点: 添加 directional 家族时, 需在此添加映射条目, 如:
#   "directional_15m_expanded": "directional_15m"
_FT_KEY_MAP = {
    "independent_15m_expanded": "independent_15m",
    "independent_1h_expanded": "independent_1h",
}


def _merge_recommendations(
    step2_candidates: dict[str, Any],
    step3_recommendations: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """合并 Step 2 基线和 Step 3 扩展推荐。

    合并策略:
    - Step 2 基础参数直接采用 (scale, cost, ticks)
    - Step 3 扩展参数直接采用 (entry/close/risk/timing/buffer)
    - 两步都有推荐时, Step 3 扩展参数优先; 基础参数取 confidence 更高者
    - 缺失参数使用 ReplayParameterOverrides 默认值

    Returns:
        {ft_key: {param_name: {value, confidence, reason, source}}}
    """
    merged: dict[str, dict[str, Any]] = {}
    s2_cands = step2_candidates.get("candidates", {})

    # 收集所有目标 ft_keys
    all_ft_keys: set[str] = set()
    for k in s2_cands:
        all_ft_keys.add(k)
    for k in step3_recommendations:
        all_ft_keys.add(_FT_KEY_MAP.get(k, k))

    for ft_key in sorted(all_ft_keys):
        m: dict[str, Any] = {}

        # (a) 先填充 Step 2 基线值
        s2 = s2_cands.get(ft_key, {})
        for pname in (_STEP2_BASE_PARAMS | _STEP3_EXPANDED_PARAMS):
            if pname in s2 and s2[pname] is not None:
                m[pname] = {
                    "value": s2[pname],
                    "confidence": "medium",
                    "reason": "来自 Step 2 baseline",
                    "source": "step2",
                }
            elif pname in _PARAM_DEFAULTS:
                m[pname] = {
                    "value": _PARAM_DEFAULTS[pname],
                    "confidence": "low",
                    "reason": "使用 ReplayParameterOverrides 默认值",
                    "source": "default",
                }

        # (b) 用 Step 3 推荐覆盖
        #     找到对应的 expanded key
        s3_key: str | None = None
        for ek, fk in _FT_KEY_MAP.items():
            if fk == ft_key:
                s3_key = ek
                break

        s3 = step3_recommendations.get(s3_key or ft_key, {})
        for pname, prec in s3.items():
            if pname.startswith("_"):
                continue
            if not isinstance(prec, dict) or "value" not in prec:
                continue
            if prec["value"] is None:
                continue

            existing = m.get(pname, {})
            existing_conf = _CONFIDENCE_RANK.get(
                existing.get("confidence", "low"), 0,
            )
            new_conf = _CONFIDENCE_RANK.get(
                prec.get("confidence", "low"), 0,
            )

            # Step 3 扩展参数始终采用 Step 3 值 (这是 Step 3 的主要贡献)
            # Step 2 基础参数只在 Step 3 置信度 >= 时覆盖
            if pname in _STEP3_EXPANDED_PARAMS or new_conf >= existing_conf:
                m[pname] = {
                    "value": prec["value"],
                    "confidence": prec.get("confidence", "low"),
                    "reason": prec.get("reason", ""),
                    "source": "step3",
                }

        merged[ft_key] = m

    return merged


# =========================================================================
# 4. 约束校验
# =========================================================================


def _validate_constraints(
    merged: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """验证合并后的参数是否满足 ReplayParameterOverrides 的约束。

    若检测到违反, 尝试自动修复 (最小幅度调整)。

    Returns:
        {"violations": [...], "auto_fixes": [...], "all_passed": bool}
    """
    violations: list[dict[str, Any]] = []
    auto_fixes: list[dict[str, Any]] = []

    for ft_key, params_dict in merged.items():
        for rule in _CONSTRAINT_RULES:
            # 每次检查前重新提取值, 确保使用前一个 auto-fix 的结果
            values = {
                k: v["value"]
                for k, v in params_dict.items()
                if isinstance(v, dict) and "value" in v
            }

            if not rule["check"](values):
                violation = {
                    "ft_key": ft_key,
                    "rule": rule["name"],
                    "description": rule["description"],
                    "values": {p: values.get(p) for p in rule["params"]},
                }
                violations.append(violation)

                fix = _auto_fix_constraint(
                    ft_key, rule["name"], values, params_dict,
                )
                if fix:
                    auto_fixes.append(fix)

    return {
        "violations": violations,
        "auto_fixes": auto_fixes,
        "all_passed": len(violations) == 0,
    }


def _auto_fix_constraint(
    ft_key: str,
    rule_name: str,
    values: dict[str, Any],
    params_dict: dict[str, Any],
) -> dict[str, Any] | None:
    """尝试自动修复约束违反。

    修复策略:
    - failed_thesis <= de_risk: 上调 de_risk 至 failed_thesis + 3.0
    - close <= entry: 下调 close 至 entry - 0.05
    - scale_in >= entry: 上调 scale_in 至 entry + 0.10
    """
    if rule_name == "failed_thesis <= de_risk":
        ft = values.get("failed_thesis_net_edge_bps", -1.0)
        new_dr = round(ft + 3.0, 4)
        old_dr = values.get("de_risk_net_edge_bps")
        params_dict["de_risk_net_edge_bps"] = {
            **params_dict.get("de_risk_net_edge_bps", {}),
            "value": new_dr,
            "reason": f"Auto-fixed: de_risk 上调至 {new_dr} (failed_thesis={ft} + 3.0)",
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "rule": rule_name,
            "param": "de_risk_net_edge_bps",
            "old": old_dr, "new": new_dr,
        }

    if rule_name == "close <= entry":
        entry = values.get("entry_threshold", 0.40)
        new_close = max(0.0, round(entry - 0.05, 4))
        old_close = values.get("close_threshold")
        params_dict["close_threshold"] = {
            **params_dict.get("close_threshold", {}),
            "value": new_close,
            "reason": f"Auto-fixed: close 下调至 {new_close} (entry={entry} - 0.05)",
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "rule": rule_name,
            "param": "close_threshold",
            "old": old_close, "new": new_close,
        }

    if rule_name == "scale_in >= entry":
        entry = values.get("entry_threshold", 0.40)
        new_si = round(entry + 0.10, 4)
        old_si = values.get("scale_in_threshold")
        params_dict["scale_in_threshold"] = {
            **params_dict.get("scale_in_threshold", {}),
            "value": new_si,
            "reason": f"Auto-fixed: scale_in 上调至 {new_si} (entry={entry} + 0.10)",
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "rule": rule_name,
            "param": "scale_in_threshold",
            "old": old_si, "new": new_si,
        }

    return None


# =========================================================================
# 5. 合并参数候选输出
# =========================================================================


def _build_merged_parameter_candidates(
    merged: dict[str, dict[str, Any]],
    constraint_result: dict[str, Any],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 parameter_candidates_merged.json。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, Any]] = {}
    pending_validation: list[str] = []

    for ft_key, params_dict in merged.items():
        c: dict[str, Any] = {}
        for pname, prec in params_dict.items():
            if isinstance(prec, dict) and "value" in prec:
                c[pname] = prec["value"]
                if prec.get("confidence") == "low":
                    pending_validation.append(f"{pname} in {ft_key}")
        candidates[ft_key] = c

    data = {
        "round_id": round_id,
        "scope": {"symbol": _SYMBOL, "step": "step3_merged"},
        "candidates": candidates,
        "pending_validation": pending_validation,
        "constraint_check": {
            "all_passed": constraint_result["all_passed"],
            "violation_count": len(constraint_result["violations"]),
            "auto_fix_count": len(constraint_result["auto_fixes"]),
        },
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Wrote parameter_candidates_merged.json -> %s", output_path)
    return output_path


# =========================================================================
# 6. 结论文档
# =========================================================================


def _build_step3_conclusion_report(
    calibration_results: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    step3_recommendations: dict[str, dict[str, Any]],
    merged: dict[str, dict[str, Any]],
    constraint_result: dict[str, Any],
    step2_baseline: dict[str, Any],
    round_id: str,
    output_path: pathlib.Path,
) -> pathlib.Path:
    """生成 phase2_step3_research_conclusion.md。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _a = lines.append
    now_str = datetime.now(timezone.utc).isoformat()

    # ── Header ──
    _a("# Phase 2 Step 3: Expanded Parameter Research Conclusion")
    _a("")
    _a(f"> Round ID: `{round_id}`")
    _a(f"> Generated at: {now_str}")
    _a("")

    # ── 1. Scope ──
    _a("## 1. Scope")
    _a("")
    # 从实际结果动态提取 family / timeframe
    _families = sorted({cr["family"] for cr in calibration_results}) or ["independent"]
    _timeframes = sorted({cr["timeframe"] for cr in calibration_results}) or ["15m", "1H"]
    _a(f"- **Symbol**: {_SYMBOL}")
    _a(f"- **Family**: {', '.join(_families)}")
    _a(f"- **Timeframes**: {', '.join(_timeframes)}")
    _a("- **Expanded Parameters**: entry_threshold, close_threshold, "
       "de_risk_net_edge_bps, failed_thesis_net_edge_bps, "
       "min_hold_seconds, rebalance_cooldown_seconds, "
       "expected_slippage_buffer_bps, expected_execution_buffer_bps")
    for cr in calibration_results:
        for br in cr.get("batch_results", []):
            s = br.get("summary")
            if s and s.get("window"):
                _a(f"- **Window**: {s['window']}")
                break
        else:
            continue
        break
    _a("")

    # ── 2. Executed Calibrations ──
    _a("## 2. What Was Executed")
    _a("")
    _a("### 2.1 Expanded Calibration Rounds")
    _a("")
    _a("| Round | Family | Timeframe | Batches | Status |")
    _a("|-------|--------|-----------|---------|--------|")
    for cr in calibration_results:
        n_batches = len(cr.get("batch_results", []))
        n_ok = sum(
            1 for b in cr.get("batch_results", [])
            if b["status"] == "succeeded"
        )
        _a(f"| {cr['round_key']} | {cr['family']} | {cr['timeframe']} "
           f"| {n_ok}/{n_batches} succeeded | {cr['status']} |")
    _a("")

    _a("### 2.2 Batch Details")
    _a("")
    for cr in calibration_results:
        _a(f"**{cr['round_key']}**:")
        _a("")
        _a("| Batch | Status | Experiments |")
        _a("|-------|--------|-------------|")
        for br in cr.get("batch_results", []):
            key = br.get("_key", "?")
            status = br["status"]
            n_exps = 0
            s = br.get("summary")
            if s:
                n_exps = s.get("succeeded", 0) + s.get("failed", 0)
            _a(f"| {key} | {status} | {n_exps} |")
        _a("")

    # ── 3. Step 3 Expanded Recommendations ──
    _a("## 3. Step 3 Expanded Recommendations")
    _a("")
    for ft_key, recs in step3_recommendations.items():
        _a(f"### {ft_key}")
        _a("")
        _a("| Parameter | Value | Confidence | Reason |")
        _a("|-----------|-------|------------|--------|")
        for pname, prec in recs.items():
            if pname.startswith("_"):
                continue
            if not isinstance(prec, dict) or "value" not in prec:
                continue
            val = prec.get("value")
            val_str = str(val) if val is not None else "*(pending)*"
            conf = prec.get("confidence", "N/A")
            reason = prec.get("reason", "")
            r_short = reason[:100] + "..." if len(reason) > 100 else reason
            _a(f"| `{pname}` | {val_str} | {conf} | {r_short} |")
        _a("")

    # ── 4. Merged Parameters ──
    _a("## 4. Merged Parameter Candidates (Step 2 + Step 3)")
    _a("")
    for ft_key, params_dict in merged.items():
        _a(f"### {ft_key}")
        _a("")
        _a("| Parameter | Value | Confidence | Source |")
        _a("|-----------|-------|------------|--------|")
        for pname, prec in sorted(params_dict.items()):
            if not isinstance(prec, dict) or "value" not in prec:
                continue
            val = prec.get("value")
            val_str = str(val) if val is not None else "*(pending)*"
            conf = prec.get("confidence", "N/A")
            source = prec.get("source", "?")
            _a(f"| `{pname}` | {val_str} | {conf} | {source} |")
        _a("")

    # ── 5. Constraint Validation ──
    _a("## 5. Constraint Validation")
    _a("")
    if constraint_result["all_passed"]:
        _a("All parameter constraints passed.")
    else:
        n_v = len(constraint_result["violations"])
        _a(f"**{n_v} violation(s) detected:**")
        _a("")
        for v in constraint_result["violations"]:
            _a(f"- **{v['ft_key']}**: `{v['rule']}` - {v['description']}")
            _a(f"  Values: {v['values']}")
        _a("")
        if constraint_result["auto_fixes"]:
            n_f = len(constraint_result["auto_fixes"])
            _a(f"**{n_f} auto-fix(es) applied:**")
            _a("")
            for af in constraint_result["auto_fixes"]:
                _a(f"- **{af['ft_key']}**: "
                   f"`{af['param']}` {af['old']} -> {af['new']}")
    _a("")

    # ── 6. Step 2 Baseline Reference ──
    _a("## 6. Step 2 Baseline Reference")
    _a("")
    s2_round = step2_baseline.get("round_id", "N/A")
    _a(f"- **Step 2 Round ID**: `{s2_round}`")
    s2_cands = step2_baseline.get("candidates", {})
    if s2_cands:
        for s2k, s2v in s2_cands.items():
            n_params = len(s2v) if isinstance(s2v, dict) else 0
            _a(f"- **{s2k}**: {n_params} params")
    else:
        _a("- *(No Step 2 baseline loaded)*")
    _a("")

    # ── 7. Stable Conclusions ──
    _a("## 7. Stable Conclusions")
    _a("")
    stable: list[str] = []
    for ft_key, params_dict in merged.items():
        for pname, prec in params_dict.items():
            if isinstance(prec, dict) and prec.get("confidence") == "high":
                val = prec.get("value")
                src = prec.get("source", "?")
                stable.append(
                    f"`{pname}` = {val} in **{ft_key}** "
                    f"(high confidence, source={src})"
                )
    for s in stable:
        _a(f"- {s}")
    if not stable:
        _a("- 无 high-confidence 结论 (需要更多数据或更长窗口)")
    _a("")

    # ── 8. Next Steps ──
    _a("## 8. Next Steps")
    _a("")
    _a("1. **Phase 3 Live Attribution**: "
       "运行 `rdp_run_phase3_round.py`, 获取归因数据")
    _a("2. **Phase 4 Execution Realism**: "
       "运行 `rdp_run_phase4_round.py`, 验证执行可行性")
    _a("3. **Phase 5 Governance + Phase 6 Decision Round**: "
       "运行 `rdp_run_decision_round.py`, 评审参数晋升")
    _a("4. 在更长时间窗口上重复验证参数稳定性")
    _a("5. 扩展到 directional 家族的扩展参数扫描")
    _a("")

    # ── 9. Caveats ──
    _a("## 9. Caveats")
    _a("")
    _a("- Phase 2 replay 使用简化评分模型 (不含 AI assessment), "
       "与生产系统评分存在偏差")
    _a("- 不包含撮合仿真、滑点模型和 orderbook realism (属于 Phase 4)")
    _a("- 持仓逻辑为简化版 (固定 1 单位), 不反映真实资金管理")
    _a("- 当前数据窗口较短, 推荐结论需在更长窗口上验证")
    _a("- 自动约束修复仅做最小调整, 实际值需人工确认")
    _a("")

    content = "\n".join(lines)
    output_path.write_text(content, encoding="utf-8")
    log.info("Wrote conclusion -> %s (%d lines)", output_path, len(lines))
    return output_path


# =========================================================================
# 主流程
# =========================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step 3 Research Orchestrator: "
                    "independent 扩展参数扫描 + 端到端合并",
    )
    parser.add_argument(
        "--artifact-root", type=str, default=str(_DEFAULT_ARTIFACT_ROOT),
        help=f"Artifact output root (default: {_DEFAULT_ARTIFACT_ROOT})",
    )
    parser.add_argument(
        "--step2-round-dir", type=str, default=None,
        help="Step 2 round directory for baseline loading. "
             "Auto-detects latest if not specified.",
    )
    parser.add_argument(
        "--ensure-schema", action="store_true",
        help="Run DB migrations before first batch",
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Stop entire round on first batch failure",
    )
    parser.add_argument(
        "--skip-calibration", action="store_true",
        help="Skip calibration phase (only run merge + aggregation)",
    )
    parser.add_argument(
        "--skip-merge", action="store_true",
        help="Skip merge phase (only run calibration + recommendations)",
    )
    parser.add_argument(
        "--no-print-summary", action="store_true",
        help="Suppress final summary to stdout",
    )
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()
    round_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_" + uuid4().hex[:8]
    )
    artifact_root = pathlib.Path(args.artifact_root)
    round_dir = artifact_root / round_id

    log.info("=" * 66)
    log.info("Step 3 Research Orchestrator")
    log.info("  Round ID  : %s", round_id)
    log.info("  Symbol    : %s", _SYMBOL)
    log.info("  Scope     : independent / 15m + 1H (expanded params)")
    log.info("  Output    : %s", round_dir)
    log.info("=" * 66)

    # ================================================================
    # Phase A: Expanded Calibration Rounds
    # ================================================================
    calibration_results: list[dict[str, Any]] = []

    if not args.skip_calibration:
        log.info("")
        log.info("=" * 66)
        log.info("Phase A: Expanded Calibration Rounds")
        log.info("=" * 66)

        batch_artifact_root = round_dir / "batches"
        first_round = True

        for round_key in _EXPANDED_ROUND_KEYS:
            cal_def = _CALIBRATION_DEFS[round_key]
            result = _run_step3_calibration_round(
                round_key, cal_def, batch_artifact_root,
                ensure_schema=args.ensure_schema and first_round,
                stop_on_error=args.stop_on_error,
            )
            calibration_results.append(result)
            first_round = False

            if result["status"] == "failed" and args.stop_on_error:
                log.error("--stop-on-error: aborting after %s", round_key)
                break
    else:
        log.info("Phase A: SKIPPED (--skip-calibration)")

    # ================================================================
    # Phase B: Aggregation + Expanded Recommendations
    # ================================================================
    log.info("")
    log.info("=" * 66)
    log.info("Phase B: Aggregation + Expanded Recommendations")
    log.info("=" * 66)

    # B.1 汇总实验数据
    all_rows = _collect_calibration_experiments(calibration_results)
    log.info("Total expanded calibration experiments: %d", len(all_rows))

    _write_family_timeframe_summary_csv(
        all_rows, round_dir / "family_timeframe_summary.csv",
    )
    _write_family_timeframe_summary_json(
        all_rows, round_id, round_dir / "family_timeframe_summary.json",
    )

    # B.2 为每个 expanded group 生成推荐
    step3_recommendations: dict[str, dict[str, Any]] = {}
    for cr in calibration_results:
        ft_key = cr["round_key"]
        recs = _generate_single_ft_recommendations(
            all_rows, cr, cr["family"], cr["timeframe"],
        )
        step3_recommendations[ft_key] = recs
        log.info("Generated expanded recommendations for %s", ft_key)

    # B.3 输出 Step 3 独立推荐
    s3_rec_path = round_dir / "step3_expanded_recommendations.json"
    s3_rec_path.parent.mkdir(parents=True, exist_ok=True)
    with s3_rec_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "round_id": round_id,
                "recommendations": {
                    k: {
                        pk: pv for pk, pv in v.items()
                        if not pk.startswith("_")
                    }
                    for k, v in step3_recommendations.items()
                },
            },
            f, indent=2, ensure_ascii=False, default=str,
        )
    log.info("Wrote step3_expanded_recommendations.json -> %s", s3_rec_path)

    # ================================================================
    # Phase C: Step 2 Baseline Loading + Merge + Constraint Validation
    # ================================================================
    merged: dict[str, dict[str, Any]] = {}
    constraint_result: dict[str, Any] = {
        "violations": [], "auto_fixes": [], "all_passed": True,
    }
    step2_baseline: dict[str, Any] = {
        "candidates": {}, "pending_validation": [],
    }

    if not args.skip_merge:
        log.info("")
        log.info("=" * 66)
        log.info("Phase C: Merge + Constraint Validation")
        log.info("=" * 66)

        # C.1 加载 Step 2 基线
        step2_dir = (
            pathlib.Path(args.step2_round_dir)
            if args.step2_round_dir else None
        )
        step2_baseline = _load_step2_baseline(step2_dir)
        log.info(
            "Step 2 baseline: %d family/timeframe groups",
            len(step2_baseline.get("candidates", {})),
        )

        # C.2 合并推荐
        merged = _merge_recommendations(step2_baseline, step3_recommendations)
        log.info("Merged parameter groups: %s", list(merged.keys()))

        # C.3 约束验证 + 自动修复
        constraint_result = _validate_constraints(merged)
        if constraint_result["all_passed"]:
            log.info("All parameter constraints passed")
        else:
            log.warning(
                "%d constraint violation(s) detected, %d auto-fixed",
                len(constraint_result["violations"]),
                len(constraint_result["auto_fixes"]),
            )

        # C.4 输出约束校验结果
        cv_path = round_dir / "constraint_violations.json"
        cv_path.parent.mkdir(parents=True, exist_ok=True)
        with cv_path.open("w", encoding="utf-8") as f:
            json.dump(
                constraint_result, f,
                indent=2, ensure_ascii=False, default=str,
            )
        log.info("Wrote constraint_violations.json -> %s", cv_path)

        # C.5 输出合并后参数候选
        _build_merged_parameter_candidates(
            merged, constraint_result, round_id,
            round_dir / "parameter_candidates_merged.json",
        )
    else:
        log.info("Phase C: SKIPPED (--skip-merge)")

    # ================================================================
    # Phase D: Conclusion Document
    # ================================================================
    log.info("")
    log.info("=" * 66)
    log.info("Phase D: Conclusion Document")
    log.info("=" * 66)

    _build_step3_conclusion_report(
        calibration_results, all_rows,
        step3_recommendations, merged, constraint_result,
        step2_baseline, round_id,
        round_dir / "phase2_step3_research_conclusion.md",
    )

    # Manifest
    finished_at = datetime.now(timezone.utc).isoformat()
    _write_manifest(
        calibration_results, [],  # Step 3 无 scan phase
        round_id, started_at, finished_at,
        round_dir / "round_manifest.json",
    )

    # ================================================================
    # 最终汇总
    # ================================================================
    cal_ok = sum(
        1 for cr in calibration_results if cr["status"] == "succeeded"
    )
    cal_partial = sum(
        1 for cr in calibration_results if cr["status"] == "partial_success"
    )
    cal_fail = sum(
        1 for cr in calibration_results if cr["status"] == "failed"
    )

    log.info("")
    log.info("=" * 66)
    log.info("Step 3 completed:")
    log.info("  Calibration: %d succeeded, %d partial, %d failed",
             cal_ok, cal_partial, cal_fail)
    log.info("  Total experiments: %d", len(all_rows))
    log.info("  Constraints: %s",
             "ALL PASSED" if constraint_result["all_passed"]
             else f"{len(constraint_result['violations'])} violations")
    log.info("  Round dir: %s", round_dir)
    log.info("=" * 66)

    if not args.no_print_summary:
        _constraint_status = (
            "ALL PASSED" if constraint_result["all_passed"]
            else f"{len(constraint_result['violations'])} violations"
        )
        print("")
        print(f"=== Step 3 Research: {round_id} ===")
        print(f"Symbol: {_SYMBOL}")
        print(f"Calibration: {cal_ok} ok, {cal_partial} partial, "
              f"{cal_fail} failed")
        print(f"Total experiments: {len(all_rows)}")
        print(f"Constraints: {_constraint_status}")
        print("")

        if merged:
            print("Merged Parameter Candidates:")
            for ft_key, params_dict in merged.items():
                print(f"\n  [{ft_key}]")
                for pname, prec in sorted(params_dict.items()):
                    if not isinstance(prec, dict) or "value" not in prec:
                        continue
                    val = prec.get("value")
                    conf = prec.get("confidence", "?")
                    src = prec.get("source", "?")
                    val_str = str(val) if val is not None else "(pending)"
                    print(f"    {pname:<38s} = "
                          f"{val_str:<12s} [{conf}] ({src})")

        print("")
        print(f"Conclusion : "
              f"{round_dir / 'phase2_step3_research_conclusion.md'}")
        print(f"Merged     : "
              f"{round_dir / 'parameter_candidates_merged.json'}")
        print(f"Constraints: "
              f"{round_dir / 'constraint_violations.json'}")
        print(f"Artifacts  : {round_dir}")

    # 退出码: 3=全部失败, 2=部分失败, 0=全部成功
    if cal_fail > 0 and cal_ok == 0 and cal_partial == 0:
        return 3
    if cal_fail > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
