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
import hashlib
import importlib.util
import json
import logging
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
)
from aats.data_platform.governance._exceptions import DBUnavailableError
from aats.data_platform.governance.research_artifact_contract import (
    require_regular_round_file,
    resolve_formal_round_dir,
    validate_calibration_child_artifacts,
    validate_scan_child_artifacts,
)
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_STEP2,
    ROUND_PHASE_STEP3,
    load_research_round_snapshot,
    research_round_snapshot_fingerprint,
    save_research_round_snapshot,
)
from aats.data_platform.governance.typed_json_identity import typed_json_sha256

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
_normalize_dataset_version = _step2._normalize_dataset_version
_SYMBOL: str = _step2._SYMBOL
_EXPECTED_STEP2_CALIBRATION_KEYS = tuple(
    _step2._EXPECTED_STEP2_CALIBRATION_KEYS
)
_EXPECTED_STEP2_SCAN_KEYS = tuple(_step2._EXPECTED_STEP2_SCAN_KEYS)
_EXPECTED_STEP2_CALIBRATION_TOPOLOGY = dict(
    _step2._EXPECTED_STEP2_CALIBRATION_TOPOLOGY
)
_EXPECTED_STEP2_SCAN_TOPOLOGY = dict(_step2._EXPECTED_STEP2_SCAN_TOPOLOGY)
_BATCH_RUN_ID_RE = _step2._BATCH_RUN_ID_RE
_UUID_RE = _step2._UUID_RE

# Step 3 校准定义（引用 Step 2 中已定义的 expanded groups）
_CALIBRATION_DEFS: dict[str, dict[str, Any]] = _step2._CALIBRATION_DEFS

# ── 常量 ──
_DEFAULT_ARTIFACT_ROOT = pathlib.Path("artifacts/research/step3_rounds")
_STEP3_RESULT_PREFIX = "RDP_STEP3_RESULT_JSON="
_STEP2_ROUND_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Step 3 只运行 independent 家族的扩展校准
_EXPANDED_ROUND_KEYS = (
    "independent_15m_expanded",
    "independent_1h_expanded",
)
_EXPECTED_STEP3_COMBO_KEYS = frozenset(
    {
        "independent_15m",
        "independent_1h",
        "directional_15m",
        "directional_1h",
    }
)
_EXPECTED_STEP3_CALIBRATION_TOPOLOGY = {
    key: (
        str(_CALIBRATION_DEFS[key]["family"]),
        str(_CALIBRATION_DEFS[key]["timeframe"]),
        tuple(str(batch["key"]) for batch in _CALIBRATION_DEFS[key]["batches"]),
    )
    for key in _EXPANDED_ROUND_KEYS
}


def _manifest_result_set_is_valid(
    results: Any,
    *,
    key_field: str,
    expected_keys: tuple[str, ...],
    require_complete_success: bool,
    expected_topology: dict[str, tuple[Any, ...]] | None = None,
) -> bool:
    if not isinstance(results, list) or not all(
        isinstance(item, dict) for item in results
    ):
        return False
    keys = [item.get(key_field) for item in results]
    statuses = [item.get("status") for item in results]
    if (
        not all(isinstance(key, str) and key for key in keys)
        or len(set(keys)) != len(keys)
        or not set(keys).issubset(expected_keys)
        or not all(
            status in {"succeeded", "partial_success", "failed"}
            for status in statuses
        )
    ):
        return False
    if require_complete_success:
        complete = bool(
            len(results) == len(expected_keys)
            and set(keys) == set(expected_keys)
            and all(status == "succeeded" for status in statuses)
        )
        if not complete:
            return False
    if expected_topology is None:
        return True
    for item in results:
        key = item[key_field]
        expected = expected_topology[key]
        if item.get("family") != expected[0] or item.get("timeframe") != expected[1]:
            return False
        if key_field == "round_key" and item.get("status") == "succeeded":
            batches = item.get("batches")
            if (
                not isinstance(batches, list)
                or not all(isinstance(batch, dict) for batch in batches)
            ):
                return False
            batch_keys = [batch.get("key") for batch in batches]
            if (
                len(batch_keys) != len(expected[2])
                or len(set(batch_keys)) != len(batch_keys)
                or set(batch_keys) != set(expected[2])
                or any(batch.get("status") != "succeeded" for batch in batches)
                or any(
                    not isinstance(batch.get("batch_run_id"), str)
                    or _BATCH_RUN_ID_RE.fullmatch(batch["batch_run_id"]) is None
                    or not isinstance(batch.get("batch_dir"), str)
                    or not isinstance(batch.get("summary_sha256"), str)
                    or _SHA256_RE.fullmatch(batch["summary_sha256"]) is None
                    or type(batch.get("summary_size_bytes")) is not int
                    or batch["summary_size_bytes"] <= 0
                    for batch in batches
                )
            ):
                return False
        if key_field == "scan_key" and item.get("status") in {
            "succeeded",
            "partial_success",
        }:
            if (
                not isinstance(item.get("scan_run_id"), str)
                or _UUID_RE.fullmatch(item["scan_run_id"]) is None
                or not isinstance(item.get("scan_dir"), str)
                or not isinstance(item.get("comparison_sha256"), str)
                or _SHA256_RE.fullmatch(item["comparison_sha256"]) is None
                or type(item.get("comparison_size_bytes")) is not int
                or item["comparison_size_bytes"] <= 0
                or not isinstance(item.get("window"), dict)
                or not isinstance(item.get("dataset_version"), str)
                or not isinstance(item.get("grid_sha256"), str)
                or _SHA256_RE.fullmatch(item["grid_sha256"]) is None
            ):
                return False
    return True


# =========================================================================
# Family-aware 参数默认值与约束规则 (P1-4 + P1-5)
# =========================================================================
#
# 背景:
#   ReplayParameterOverrides.for_family("directional") 使用与 independent 不同
#   的默认阈值 (entry=0.45/close=0.20 vs independent 的 0.40/0.15)。Step 3
#   merge + constraint 逻辑原本硬编码 independent 默认值，导致:
#     - directional 家族缺失参数时回填了 independent 默认值
#     - 约束校验/自动修复使用错误 fallback（如 entry_threshold=0.40），
#       在 directional 上下文中产生不合理的自动调整
#
# 解决方案:
#   将 _PARAM_DEFAULTS 和 _CONSTRAINT_RULES 从模块级常量重构为 family-aware
#   工厂函数 _get_param_defaults(family) / _get_constraint_rules(family)。
#   所有调用点从 ft_key 提取 family 后传入。
#
# 真相源:
#   _DEFAULTS_BY_FAMILY 必须与 ReplayParameterOverrides.for_family() 保持一致，
#   修改任何一侧都需要同步更新。

# independent 家族默认值（与 ReplayParameterOverrides() 空构造对齐）
_INDEPENDENT_DEFAULTS: dict[str, float | int | None] = {
    "signal_edge_scale_bps": 12.0,
    "taker_fee_bps": 5.0,
    "slippage_bps": 1.0,
    "min_confirm_ticks": 2,
    "min_safe_net_edge_bps": 2.0,
    "score_stability_threshold": 5.0,
    "entry_threshold": 0.30,
    "close_threshold": 0.15,
    "scale_in_threshold": 0.40,
    "de_risk_net_edge_bps": 2.0,
    "failed_thesis_net_edge_bps": -1.0,
    "catastrophic_failed_thesis_buffer_bps": 3.0,
    "min_hold_seconds": 300.0,
    "rebalance_cooldown_seconds": 120.0,
    "expected_slippage_buffer_bps": 0.5,
    "expected_execution_buffer_bps": 0.5,
    "max_thesis_age_seconds": 1800.0,
    "max_acceptable_cost_bps": 7.5,
    "min_score_drawdown_bps": 6.0,
    "min_liquidity_quality": 0.55,
    "limit_offset_bps_entry": 1.5,
    "noise_buffer_bps": 2.0,
    "maker_fee_bps": 2.0,
    "execution_style": "passive_first",
    "passive_bias": 0.7,
    "directional_trend_weight": 0.7,
    "directional_return_clamp_bps": 20.0,
    # short 阈值默认 None，仅 directional 家族使用
    "short_entry_threshold": None,
    "short_close_threshold": None,
}

# directional 家族默认值（与 ReplayParameterOverrides.for_family("directional") 对齐）
# 差异: entry_threshold 0.30→0.45, close_threshold 0.15→0.20, scale_in_threshold 0.40→0.55
# 修正记录（2026-04-21）：_DIRECTIONAL_DEFAULTS 原本漏掉 scale_in_threshold=0.55，
# 导致 step3 合并 fallback 给 directional 继承 _INDEPENDENT_DEFAULTS["scale_in_threshold"]=0.40，
# 与 replay 侧 ReplayParameterOverrides.for_family("directional").scale_in_threshold=0.55
# 静默偏差。复审的真源对齐（replay_context.py:342）发现后补齐。
# 影响面：directional 家族缺失 scale_in_threshold 时，约束检查 fallback 偏低，
# 可能掩盖合理约束违反；replay 侧读 0.55 而 step3 写 0.40，两套默认不一致。
_DIRECTIONAL_DEFAULTS: dict[str, float | int | None] = {
    **_INDEPENDENT_DEFAULTS,
    "entry_threshold": 0.45,
    "close_threshold": 0.20,
    "scale_in_threshold": 0.55,
}

_DEFAULTS_BY_FAMILY: dict[str, dict[str, float | int | None]] = {
    "independent": _INDEPENDENT_DEFAULTS,
    "directional": _DIRECTIONAL_DEFAULTS,
}
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _family_from_ft_key(ft_key: str) -> str:
    """从 ft_key (如 'independent_15m' / 'directional_1h_expanded') 提取 family。

    支持:
      - 'independent_15m' -> 'independent'
      - 'directional_1h_expanded' -> 'directional'
      - 无已知前缀时默认 'independent' 以保持向后兼容
    """
    lowered = ft_key.lower()
    if lowered.startswith("directional"):
        return "directional"
    if lowered.startswith("independent"):
        return "independent"
    return "independent"


def _get_param_defaults(family: str = "independent") -> dict[str, float | int | None]:
    """获取指定 family 的默认参数字典。

    与 ReplayParameterOverrides.for_family(family) 一一对应，修改任何一侧
    需要同步更新另一侧，否则 Step 3 merge 与 replay 的默认值会出现静默偏差。
    """
    return _DEFAULTS_BY_FAMILY.get(family, _INDEPENDENT_DEFAULTS)


def _get_effective_param_defaults(ft_key: str) -> dict[str, float | int | None]:
    family = _family_from_ft_key(ft_key)
    defaults = dict(_get_param_defaults(family))
    try:
        from aats.data_platform.operations.strategy_tuning_registry import (
            get_combo_tuning_overrides,
        )

        timeframe = ft_key.split("_", 1)[1] if "_" in ft_key else ""
        overrides = get_combo_tuning_overrides(_PROJECT_ROOT, family, timeframe)
        for key, value in overrides.items():
            if key in defaults and value is not None:
                defaults[key] = value
    except Exception:  # pragma: no cover - best effort only
        pass
    return defaults


def _get_constraint_rules(family: str = "independent") -> list[dict[str, Any]]:
    """返回 family-aware 约束规则列表。

    所有约束默认 fallback 使用对应 family 的默认值，例如:
      - independent: close_threshold fallback = 0.10, entry_threshold = 0.30
      - directional: close_threshold fallback = 0.20, entry_threshold = 0.45

    这样在部分参数缺失时，约束检查不会因为错误的 fallback 而误判。
    """
    d = _get_param_defaults(family)

    rules: list[dict[str, Any]] = [
        {
            "name": "failed_thesis <= de_risk",
            "check": lambda c, _d=d: (
                c.get("failed_thesis_net_edge_bps", _d["failed_thesis_net_edge_bps"])
                <= c.get("de_risk_net_edge_bps", _d["de_risk_net_edge_bps"])
            ),
            "params": ["failed_thesis_net_edge_bps", "de_risk_net_edge_bps"],
            "description": "failed_thesis_net_edge_bps 必须 <= de_risk_net_edge_bps",
        },
        {
            "name": "close <= entry",
            "check": lambda c, _d=d: (
                c.get("close_threshold", _d["close_threshold"])
                <= c.get("entry_threshold", _d["entry_threshold"])
            ),
            "params": ["close_threshold", "entry_threshold"],
            "description": "close_threshold 必须 <= entry_threshold",
        },
        {
            "name": "scale_in >= entry",
            "check": lambda c, _d=d: (
                c.get("scale_in_threshold", _d["scale_in_threshold"])
                >= c.get("entry_threshold", _d["entry_threshold"])
            ),
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
        {
            "name": "safe_edge > de_risk",
            # 要求 safe_edge >= de_risk + 1.0 bps 最小间距
            # 目的: 持仓区间 [safe_edge, ∞) 与 de_risk 区间 (-∞, de_risk] 之间至少
            # 有 1.0 bps 的 hysteresis 带，避免边际信号反复触发 entry/de_risk 翻转
            "check": lambda c, _d=d: (
                c.get("min_safe_net_edge_bps", _d["min_safe_net_edge_bps"])
                + c.get("expected_slippage_buffer_bps", _d["expected_slippage_buffer_bps"])
                + c.get("expected_execution_buffer_bps", _d["expected_execution_buffer_bps"])
            ) >= c.get("de_risk_net_edge_bps", _d["de_risk_net_edge_bps"]) + 1.0,
            "params": [
                "min_safe_net_edge_bps",
                "expected_slippage_buffer_bps",
                "expected_execution_buffer_bps",
                "de_risk_net_edge_bps",
            ],
            "description": (
                "safe_edge (min_safe + slippage_buffer + exec_buffer) "
                "必须 >= de_risk_net_edge_bps + 1.0 (至少 1 bps hysteresis 带)"
            ),
        },
        {
            "name": "min_hold <= max_thesis_age",
            "check": lambda c, _d=d: (
                c.get("min_hold_seconds", _d["min_hold_seconds"])
                <= c.get("max_thesis_age_seconds", _d["max_thesis_age_seconds"])
            ),
            "params": ["min_hold_seconds", "max_thesis_age_seconds"],
            "description": (
                "min_hold_seconds 必须 <= max_thesis_age_seconds，"
                "否则 min_hold 锁定期间 stale_thesis 无法触发正常退出"
            ),
        },
        {
            "name": "catastrophic_buffer >= 0",
            "check": lambda c, _d=d: (
                c.get(
                    "catastrophic_failed_thesis_buffer_bps",
                    _d["catastrophic_failed_thesis_buffer_bps"],
                ) >= 0.0
            ),
            "params": ["catastrophic_failed_thesis_buffer_bps"],
            "description": (
                "catastrophic_failed_thesis_buffer_bps 必须 >= 0，"
                "用于 whipsaw 防护：只有跨越此缓冲才豁免 min_hold 紧急止损"
            ),
        },
    ]
    return rules


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
    start: str | None = None,
    end: str | None = None,
    dataset_version: str | None = None,
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
            start=start, end=end, dataset_version=dataset_version,
            expected_family=family,
            expected_timeframe=timeframe,
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
    n_partial = sum(1 for b in batch_results if b["status"] == "partial_success")
    n_fail = sum(1 for b in batch_results if b["status"] == "failed")
    if n_fail > 0 and n_ok == 0 and n_partial == 0:
        round_status = "failed"
    elif n_fail > 0 or n_partial > 0:
        round_status = "partial_success"
    else:
        round_status = "succeeded"

    return {
        "round_key": round_key,
        "family": family,
        "timeframe": timeframe,
        "batch_results": batch_results,
        "status": round_status,
    }


# =========================================================================
# 2. Step 2 基线加载
# =========================================================================


def _load_step2_baseline(
    step2_round_dir: pathlib.Path | None = None,
    step2_artifact_root: pathlib.Path | None = None,
    *,
    expected_dataset_version: str | None = None,
    expected_symbol: str = _SYMBOL,
    expected_window: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """加载 Step 2 的 parameter_candidates.json 作为基线。

    优先使用显式指定的 round_dir，否则自动查找最新的 step2 round。

    Returns:
        完整的 parameter_candidates 数据。未找到则返回空结构。
    """
    def _missing() -> dict[str, Any]:
        return {
            "candidates": {},
            "pending_validation": [],
            "_validated_provenance": {"status": "missing"},
        }

    def _parse_aware_time(raw: Any) -> datetime:
        if not isinstance(raw, str) or raw != raw.strip() or not raw:
            raise ValueError("step2_manifest_time_invalid")
        token = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(token)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("step2_manifest_time_timezone_missing")
        return parsed.astimezone(timezone.utc)

    def _validated(round_dir: pathlib.Path) -> dict[str, Any]:
        round_dir, project_root = resolve_formal_round_dir(
            round_dir,
            phase_dir_name="step2_rounds",
        )
        round_id = round_dir.name
        if _STEP2_ROUND_ID_RE.fullmatch(round_id) is None:
            raise ValueError("step2_round_id_invalid")
        candidates_path = round_dir / "parameter_candidates.json"
        manifest_path = round_dir / "round_manifest.json"
        candidate_bytes = require_regular_round_file(
            candidates_path,
            parent=round_dir,
        )
        manifest_bytes = require_regular_round_file(
            manifest_path,
            parent=round_dir,
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        data = json.loads(candidate_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or not isinstance(data, dict):
            raise ValueError("step2_artifact_shape_invalid")
        if (
            manifest.get("schema_version") != "aats.step2_round.v1"
            or manifest.get("phase") != "step2"
            or manifest.get("round_id") != round_id
            or manifest.get("status") not in {"succeeded", "partial_success"}
            or data.get("schema_version") != "aats.step2_candidates.v1"
            or data.get("round_id") != round_id
            or not isinstance(data.get("candidates"), dict)
        ):
            raise ValueError("step2_artifact_identity_invalid")
        require_complete_step2 = manifest.get("status") == "succeeded"
        if not _manifest_result_set_is_valid(
            manifest.get("calibrations"),
            key_field="round_key",
            expected_keys=_EXPECTED_STEP2_CALIBRATION_KEYS,
            require_complete_success=require_complete_step2,
            expected_topology=_EXPECTED_STEP2_CALIBRATION_TOPOLOGY,
        ) or not _manifest_result_set_is_valid(
            manifest.get("scans"),
            key_field="scan_key",
            expected_keys=_EXPECTED_STEP2_SCAN_KEYS,
            require_complete_success=require_complete_step2,
            expected_topology=_EXPECTED_STEP2_SCAN_TOPOLOGY,
        ):
            raise ValueError("step2_artifact_result_topology_invalid")
        symbol = manifest.get("symbol")
        dataset_version = manifest.get("dataset_version")
        manifest_scope = manifest.get("scope")
        candidate_scope = data.get("scope")
        input_refs = manifest.get("input_refs")
        window = manifest_scope.get("window") if isinstance(manifest_scope, dict) else None
        candidate_keys = set(data.get("candidates", {}))
        candidate_values = data.get("candidates", {})
        pending_validation = data.get("pending_validation")
        declared_candidate_keys = (
            candidate_scope.get("combo_keys")
            if isinstance(candidate_scope, dict)
            else None
        )
        declared_manifest_keys = (
            manifest_scope.get("combo_keys")
            if isinstance(manifest_scope, dict)
            else None
        )
        if (
            not isinstance(symbol, str)
            or symbol != expected_symbol
            or not isinstance(dataset_version, str)
            or not dataset_version
            or not isinstance(manifest_scope, dict)
            or manifest_scope.get("symbol") != symbol
            or not isinstance(candidate_scope, dict)
            or candidate_scope.get("symbol") != symbol
            or candidate_scope.get("step") != "step2_candidates"
            or not isinstance(declared_candidate_keys, list)
            or len(declared_candidate_keys) != len(set(declared_candidate_keys))
            or set(declared_candidate_keys) != candidate_keys
            or candidate_scope.get("combo_count") != len(candidate_keys)
            or not candidate_keys.issubset(_EXPECTED_STEP3_COMBO_KEYS)
            or any(
                not isinstance(values, dict) or not values
                for values in candidate_values.values()
            )
            or not isinstance(pending_validation, list)
            or any(
                not isinstance(item, str)
                or not item.strip()
                or item != item.strip()
                for item in pending_validation
            )
            or len(pending_validation) != len(set(pending_validation))
            or (
                manifest.get("status") == "succeeded"
                and candidate_keys
                != (_EXPECTED_STEP3_COMBO_KEYS - {"independent_15m"})
            )
            or (
                manifest.get("status") == "succeeded"
                and bool(pending_validation)
            )
            or not isinstance(declared_manifest_keys, list)
            or set(declared_manifest_keys) != candidate_keys
            or manifest_scope.get("combo_count") != len(candidate_keys)
            or data.get("dataset_version") != dataset_version
            or not isinstance(input_refs, dict)
            or input_refs.get("dataset_version") != dataset_version
            or input_refs.get("window") != window
            or not isinstance(window, dict)
            or set(window) != {"start", "end"}
            or not isinstance(window.get("start"), str)
            or not window["start"].strip()
            or not isinstance(window.get("end"), str)
            or not window["end"].strip()
            or (expected_window is not None and window != expected_window)
            or (
                expected_dataset_version is not None
                and dataset_version != expected_dataset_version
            )
        ):
            raise ValueError("step2_artifact_scope_invalid")
        validate_calibration_child_artifacts(
            round_dir=round_dir,
            calibrations=manifest.get("calibrations"),
            expected_topology=_EXPECTED_STEP2_CALIBRATION_TOPOLOGY,
            symbol=symbol,
            dataset_version=dataset_version,
            window=window,
        )
        validate_scan_child_artifacts(
            project_root=project_root,
            scans=manifest.get("scans"),
            expected_topology=_EXPECTED_STEP2_SCAN_TOPOLOGY,
            symbol=symbol,
            dataset_version=dataset_version,
            window=window,
        )
        digest_map = manifest.get("artifact_sha256")
        size_map = manifest.get("artifact_size_bytes")
        declared_digest = (
            digest_map.get(candidates_path.name)
            if isinstance(digest_map, dict)
            else None
        )
        declared_size = (
            size_map.get(candidates_path.name)
            if isinstance(size_map, dict)
            else None
        )
        if (
            not isinstance(declared_digest, str)
            or _SHA256_RE.fullmatch(declared_digest) is None
            or type(declared_size) is not int
            or declared_size != len(candidate_bytes)
            or hashlib.sha256(candidate_bytes).hexdigest() != declared_digest
        ):
            raise ValueError("step2_candidate_digest_invalid")
        started_at = _parse_aware_time(manifest.get("started_at"))
        finished_at = _parse_aware_time(manifest.get("finished_at"))
        round_started_at = datetime.strptime(
            round_id[:15], "%Y%m%d_%H%M%S"
        ).replace(tzinfo=timezone.utc)
        if (
            finished_at < started_at
            or finished_at > datetime.now(timezone.utc)
            or abs((started_at - round_started_at).total_seconds()) > 2.0
        ):
            raise ValueError("step2_manifest_time_invalid")
        data["_validated_provenance"] = {
            "round_id": round_id,
            "status": manifest["status"],
            "symbol": symbol,
            "dataset_version": dataset_version,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "candidate_sha256": declared_digest,
            "window": dict(window),
        }
        return data

    # 优先：显式指定的目录。存在却不可信时失败关闭，不回退其他 round。
    if step2_round_dir is not None and not step2_round_dir.exists():
        raise ValueError("step2_baseline_contract_invalid")
    if step2_round_dir is not None:
        try:
            data = _validated(step2_round_dir)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("step2_baseline_contract_invalid") from exc
        log.info("Loaded validated Step 2 baseline from %s", step2_round_dir)
        return data

    # 自动查找最新的 step2 round (使用脚本位置推断项目根, 避免 CWD 依赖)
    _project_root = pathlib.Path(__file__).resolve().parent.parent
    root = step2_artifact_root or (_project_root / "artifacts" / "research" / "step2_rounds")
    if root.exists():
        round_dirs = [item for item in root.iterdir() if item.is_dir()]
        if any(
            round_dir.is_symlink()
            or _STEP2_ROUND_ID_RE.fullmatch(round_dir.name) is None
            for round_dir in round_dirs
        ):
            raise ValueError("step2_baseline_contract_invalid")
        rounds: list[tuple[datetime, pathlib.Path]] = []
        for round_dir in round_dirs:
            if not round_dir.is_dir() or not _STEP2_ROUND_ID_RE.fullmatch(
                round_dir.name
            ):
                continue
            try:
                generated_at = datetime.strptime(
                    round_dir.name[:15], "%Y%m%d_%H%M%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ValueError("step2_baseline_contract_invalid") from exc
            rounds.append((generated_at, round_dir))
        if rounds:
            newest_second = max(item[0] for item in rounds)
            newest_group = [
                round_dir
                for generated_at, round_dir in rounds
                if generated_at == newest_second
            ]
            validated: list[tuple[datetime, dict[str, Any]]] = []
            for round_dir in newest_group:
                try:
                    data = _validated(round_dir)
                    started_at = datetime.fromisoformat(
                        data["_validated_provenance"]["started_at"]
                    )
                except (
                    OSError,
                    UnicodeDecodeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    raise ValueError("step2_baseline_contract_invalid") from exc
                validated.append((started_at, data))
            latest_data = max(validated, key=lambda item: item[0])[1]
            latest_started_at = max(item[0] for item in validated)
            if sum(item[0] == latest_started_at for item in validated) != 1:
                raise ValueError("step2_baseline_contract_invalid")
            log.info(
                "Auto-detected validated Step 2 baseline: %s",
                latest_data["_validated_provenance"]["round_id"],
            )
            return latest_data
        if round_dirs:
            raise ValueError("step2_baseline_contract_invalid")

    log.warning("No Step 2 baseline found. Merge phase will use defaults.")
    return _missing()


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
    "catastrophic_failed_thesis_buffer_bps",
    "min_hold_seconds",
    "rebalance_cooldown_seconds",
    "expected_slippage_buffer_bps",
    "expected_execution_buffer_bps",
    # 以下参数虽未做 Step 3 专项扫描，但需纳入合并输出和约束校验
    "max_thesis_age_seconds",
    "max_acceptable_cost_bps",
    "min_score_drawdown_bps",
    "min_liquidity_quality",
    "limit_offset_bps_entry",
    "directional_trend_weight",
    "directional_return_clamp_bps",
    "short_entry_threshold",
    "short_close_threshold",
})

# 向后兼容别名（如有外部测试或脚本仍 import _PARAM_DEFAULTS / _CONSTRAINT_RULES）
# 新代码应使用 _get_param_defaults(family) / _get_constraint_rules(family)
_PARAM_DEFAULTS: dict[str, float | int | None] = _INDEPENDENT_DEFAULTS
_CONSTRAINT_RULES: list[dict[str, Any]] = _get_constraint_rules("independent")

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# expanded round key -> 输出用 ft_key 的映射
#
# 当前 Step 3 只对 independent 家族跑扩展校准 (见 _EXPANDED_ROUND_KEYS),
# directional 条目预填以确保:
#   1. 当 _CALIBRATION_DEFS 引入 directional_*_expanded round 时,
#      _merge_recommendations 能直接将其映射到正确 ft_key
#   2. 单元测试 14c 已验证 _merge_recommendations 对 directional ft_key
#      使用 directional family-aware 默认 (entry=0.45/close=0.20)
#   3. 任何引入 directional expanded round 的 PR 只需扩展 _EXPANDED_ROUND_KEYS,
#      无需同时修改本映射 (避免遗漏)
_FT_KEY_MAP = {
    "independent_15m_expanded": "independent_15m",
    "independent_1h_expanded": "independent_1h",
    # TODO(directional-expansion): 当 directional family 引入 expanded
    # 校准时 (Phase 3+), 将下列条目纳入 _EXPANDED_ROUND_KEYS。
    "directional_15m_expanded": "directional_15m",
    "directional_1h_expanded": "directional_1h",
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
    - 缺失参数使用 family-aware ReplayParameterOverrides 默认值
      (independent: entry=0.40/close=0.15; directional: entry=0.45/close=0.20)

    Returns:
        {ft_key: {param_name: {value, confidence, reason, source}}}
    """
    merged: dict[str, dict[str, Any]] = {}
    s2_cands = step2_candidates.get("candidates", {})
    step2_pending = {
        str(item)
        for item in step2_candidates.get("pending_validation", [])
        if isinstance(item, str) and item.strip()
    }

    # 收集所有目标 ft_keys
    all_ft_keys: set[str] = set()
    for k in s2_cands:
        all_ft_keys.add(k)
    for k in step3_recommendations:
        all_ft_keys.add(_FT_KEY_MAP.get(k, k))

    for ft_key in sorted(all_ft_keys):
        m: dict[str, Any] = {}
        family = _family_from_ft_key(ft_key)
        family_defaults = _get_effective_param_defaults(ft_key)

        # (a) 先填充 Step 2 基线值
        s2 = s2_cands.get(ft_key, {})
        for pname in (_STEP2_BASE_PARAMS | _STEP3_EXPANDED_PARAMS):
            if pname in s2 and s2[pname] is not None:
                pending_key = f"{pname} in {ft_key}"
                m[pname] = {
                    "value": s2[pname],
                    "confidence": (
                        "low" if pending_key in step2_pending else "medium"
                    ),
                    "reason": (
                        "来自 Step 2 baseline（待验证）"
                        if pending_key in step2_pending
                        else "来自 Step 2 baseline"
                    ),
                    "source": "step2",
                }
            elif pname in family_defaults:
                m[pname] = {
                    "value": family_defaults[pname],
                    "confidence": "low",
                    "reason": (
                        f"使用 ReplayParameterOverrides.for_family("
                        f"{family!r}) 默认值"
                    ),
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

        # independent 的 replay 默认允许 short 阈值为 None（运行时跟随 long），
        # 但生产引擎的 short 字段是独立、非空设置。若候选不把 replay 的
        # resolved 语义物化，发布后生产会继续使用 profile short 默认值，造成
        # replay/资本行为分裂。这里在治理候选形成前显式固化等价数值。
        if family == "independent":
            resolved_pairs = (
                ("short_entry_threshold", "entry_threshold"),
                ("short_close_threshold", "close_threshold"),
            )
            for short_name, long_name in resolved_pairs:
                short_record = m.get(short_name)
                if not isinstance(short_record, dict) or short_record.get("value") is None:
                    long_record = m.get(long_name)
                    long_value = (
                        long_record.get("value")
                        if isinstance(long_record, dict)
                        else None
                    )
                    if long_value is not None:
                        m[short_name] = {
                            "value": long_value,
                            "confidence": long_record.get("confidence", "low"),
                            "reason": (
                                f"物化 ReplayParameterOverrides 的 {short_name} "
                                f"缺失时跟随 {long_name} 语义"
                            ),
                            "source": "resolved_replay_semantics",
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

    每个 ft_key 使用其 family 对应的 constraint rules 和默认 fallback
    (避免 directional 家族上下文中使用 independent 的 entry=0.40 兜底)。
    若检测到违反, 尝试自动修复 (最小幅度调整)。

    Returns:
        {"violations": [...], "auto_fixes": [...], "all_passed": bool}
    """
    violations: list[dict[str, Any]] = []
    auto_fixes: list[dict[str, Any]] = []

    for ft_key, params_dict in merged.items():
        family = _family_from_ft_key(ft_key)
        rules = _get_constraint_rules(family)

        for rule in rules:
            # 每次检查前重新提取值, 确保使用前一个 auto-fix 的结果
            values = {
                k: v["value"]
                for k, v in params_dict.items()
                if isinstance(v, dict) and "value" in v
                and v["value"] is not None
            }

            if not rule["check"](values):
                violation = {
                    "ft_key": ft_key,
                    "family": family,
                    "rule": rule["name"],
                    "description": rule["description"],
                    "values": {p: values.get(p) for p in rule["params"]},
                }
                violations.append(violation)

                fix = _auto_fix_constraint(
                    ft_key, rule["name"], values, params_dict,
                    family=family,
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
    *,
    family: str = "independent",
) -> dict[str, Any] | None:
    """尝试自动修复约束违反。

    修复策略:
    - failed_thesis <= de_risk: 上调 de_risk 至 failed_thesis + 3.0
    - close <= entry: 下调 close 至 entry - 0.05
    - scale_in >= entry: 上调 scale_in 至 entry + 0.10
    - safe_edge > de_risk: 上调 min_safe 使 safe_edge = de_risk + 1.0
    - min_hold <= max_thesis_age: 下调 min_hold 至 max_thesis_age

    所有 fallback 值均取自 family-aware 默认字典，避免 directional 家族
    回退到 independent 的错误基线。
    """
    d = _get_param_defaults(family)

    if rule_name == "failed_thesis <= de_risk":
        ft = values.get("failed_thesis_net_edge_bps", d["failed_thesis_net_edge_bps"])
        new_dr = round(ft + 3.0, 4)
        old_dr = values.get("de_risk_net_edge_bps")
        params_dict["de_risk_net_edge_bps"] = {
            **params_dict.get("de_risk_net_edge_bps", {}),
            "value": new_dr,
            "reason": f"Auto-fixed: de_risk 上调至 {new_dr} (failed_thesis={ft} + 3.0)",
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "family": family, "rule": rule_name,
            "param": "de_risk_net_edge_bps",
            "old": old_dr, "new": new_dr,
        }

    if rule_name == "close <= entry":
        entry = values.get("entry_threshold", d["entry_threshold"])
        new_close = max(0.0, round(entry - 0.05, 4))
        old_close = values.get("close_threshold")
        params_dict["close_threshold"] = {
            **params_dict.get("close_threshold", {}),
            "value": new_close,
            "reason": f"Auto-fixed: close 下调至 {new_close} (entry={entry} - 0.05)",
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "family": family, "rule": rule_name,
            "param": "close_threshold",
            "old": old_close, "new": new_close,
        }

    if rule_name == "scale_in >= entry":
        entry = values.get("entry_threshold", d["entry_threshold"])
        new_si = round(entry + 0.10, 4)
        old_si = values.get("scale_in_threshold")
        params_dict["scale_in_threshold"] = {
            **params_dict.get("scale_in_threshold", {}),
            "value": new_si,
            "reason": f"Auto-fixed: scale_in 上调至 {new_si} (entry={entry} + 0.10)",
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "family": family, "rule": rule_name,
            "param": "scale_in_threshold",
            "old": old_si, "new": new_si,
        }

    if rule_name == "safe_edge > de_risk":
        de_risk = values.get("de_risk_net_edge_bps", d["de_risk_net_edge_bps"])
        slip = values.get(
            "expected_slippage_buffer_bps", d["expected_slippage_buffer_bps"],
        )
        exe = values.get(
            "expected_execution_buffer_bps", d["expected_execution_buffer_bps"],
        )
        # 上调 min_safe 使 safe_edge = de_risk + 1.0
        new_min_safe = round(de_risk + 1.0 - slip - exe, 4)
        new_min_safe = max(new_min_safe, 0.0)
        old_min_safe = values.get("min_safe_net_edge_bps")
        params_dict["min_safe_net_edge_bps"] = {
            **params_dict.get("min_safe_net_edge_bps", {}),
            "value": new_min_safe,
            "reason": (
                f"Auto-fixed: min_safe 上调至 {new_min_safe} "
                f"使 safe_edge={new_min_safe}+{slip}+{exe}"
                f"={round(new_min_safe + slip + exe, 4)} > de_risk={de_risk}"
            ),
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "family": family, "rule": rule_name,
            "param": "min_safe_net_edge_bps",
            "old": old_min_safe, "new": new_min_safe,
        }

    if rule_name == "min_hold <= max_thesis_age":
        max_age = values.get("max_thesis_age_seconds", d["max_thesis_age_seconds"])
        old_hold = values.get("min_hold_seconds")
        new_hold = max_age
        params_dict["min_hold_seconds"] = {
            **params_dict.get("min_hold_seconds", {}),
            "value": new_hold,
            "reason": (
                f"Auto-fixed: min_hold 下调至 {new_hold} "
                f"(= max_thesis_age_seconds)"
            ),
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "family": family, "rule": rule_name,
            "param": "min_hold_seconds",
            "old": old_hold, "new": new_hold,
        }

    if rule_name == "short_close <= short_entry":
        s_entry = values.get("short_entry_threshold")
        if s_entry is None:
            return None
        new_s_close = max(0.0, round(s_entry - 0.05, 4))
        old_s_close = values.get("short_close_threshold")
        params_dict["short_close_threshold"] = {
            **params_dict.get("short_close_threshold", {}),
            "value": new_s_close,
            "reason": (
                f"Auto-fixed: short_close 下调至 {new_s_close} "
                f"(short_entry={s_entry} - 0.05)"
            ),
            "source": "auto_fix",
        }
        return {
            "ft_key": ft_key, "family": family, "rule": rule_name,
            "param": "short_close_threshold",
            "old": old_s_close, "new": new_s_close,
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
    *,
    dataset_version: str = "v1.0",
) -> pathlib.Path:
    """生成 parameter_candidates_merged.json。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, Any]] = {}
    pending_validation: list[str] = []

    for ft_key, params_dict in merged.items():
        c: dict[str, Any] = {}
        for pname, prec in params_dict.items():
            if isinstance(prec, dict) and "value" in prec:
                if prec["value"] is None:
                    pending_validation.append(f"{pname} in {ft_key}")
                    continue
                c[pname] = prec["value"]
                if prec.get("confidence") == "low":
                    pending_validation.append(f"{pname} in {ft_key}")
        if c:
            candidates[ft_key] = c
        else:
            pending_validation.append(f"candidate set missing in {ft_key}")
            log.warning(
                "Skip empty merged parameter candidate set for %s; governance import must treat it as unavailable",
                ft_key,
            )

    combo_keys = sorted(candidates)
    data = {
        "schema_version": "aats.step3_candidates.v1",
        "round_id": round_id,
        "dataset_version": dataset_version,
        "scope": {
            "symbol": _SYMBOL,
            "step": "step3_merged",
            "combo_keys": combo_keys,
            "combo_count": len(combo_keys),
        },
        "candidates": candidates,
        "pending_validation": pending_validation,
        "constraint_check": {
            "all_passed": constraint_result["all_passed"],
            "violation_count": len(constraint_result["violations"]),
            "auto_fix_count": len(constraint_result["auto_fixes"]),
        },
    }

    immutable_json_write(data, output_path)
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


def _determine_step3_round_status(
    *,
    calibration_results: list[dict[str, Any]],
    skip_calibration: bool,
    skip_merge: bool,
    step2_baseline: dict[str, Any],
    constraint_result: dict[str, Any],
    candidate_payload: dict[str, Any] | None,
) -> str:
    """Return the publishability status for one complete Step 3 round.

    ``succeeded`` is deliberately narrow: every expected calibration, an
    explicitly validated succeeded Step 2 baseline, a complete candidate
    scope, no pending/default evidence, and a clean constraint pass must all
    describe the same immutable round.  Any usable but incomplete result is
    ``partial_success`` and is therefore imported only as ``draft``.
    """

    statuses = [str(item.get("status") or "") for item in calibration_results]
    if statuses and all(status == "failed" for status in statuses):
        return "failed"

    calibration_keys = {
        str(item.get("round_key") or "") for item in calibration_results
    }
    full_calibration = bool(
        not skip_calibration
        and len(calibration_results) == len(_EXPANDED_ROUND_KEYS)
        and len(calibration_keys) == len(calibration_results)
        and calibration_keys == set(_EXPANDED_ROUND_KEYS)
        and statuses
        and all(status == "succeeded" for status in statuses)
    )
    provenance = step2_baseline.get("_validated_provenance")
    candidates = (
        candidate_payload.get("candidates")
        if isinstance(candidate_payload, dict)
        else None
    )
    pending = (
        candidate_payload.get("pending_validation")
        if isinstance(candidate_payload, dict)
        else None
    )
    clean_constraints = bool(
        constraint_result.get("all_passed") is True
        and not constraint_result.get("violations")
        and not constraint_result.get("auto_fixes")
    )
    complete_candidates = bool(
        isinstance(candidates, dict)
        and set(candidates) == _EXPECTED_STEP3_COMBO_KEYS
        and all(isinstance(values, dict) and values for values in candidates.values())
        and isinstance(pending, list)
        and not pending
    )
    trusted_upstream = bool(
        isinstance(provenance, dict)
        and provenance.get("status") == "succeeded"
    )
    if (
        full_calibration
        and not skip_merge
        and trusted_upstream
        and complete_candidates
        and clean_constraints
    ):
        return "succeeded"
    return "partial_success"


def _publish_managed_step3_snapshot(
    *,
    project_root: pathlib.Path,
    round_dir: pathlib.Path,
    manifest_path: pathlib.Path,
    candidate_path: pathlib.Path,
    candidate_payload: dict[str, Any] | None,
    round_id: str,
    round_status: str,
    started_at: str,
    finished_at: str,
    step2_provenance: dict[str, Any],
    conclusion_path: pathlib.Path,
) -> bool:
    """Publish the immutable Step 3 root before exposing a success marker.

    File-only development keeps its historical behavior.  Once a managed
    governance DB is configured, however, the Step 2 parent and the Step 3
    candidate must both be anchored in DB; a missing/conflicting snapshot is a
    publication failure, not permission to fall back to mutable files.
    """

    project_root = project_root.resolve()
    if not has_explicit_governance_db_configuration(project_root):
        return True
    expected_round_dir = (
        project_root / "artifacts" / "research" / "step3_rounds" / round_id
    )
    try:
        resolved_round_dir = round_dir.resolve(strict=True)
        resolved_manifest = manifest_path.resolve(strict=True)
        resolved_candidate = candidate_path.resolve(strict=True)
        if (
            resolved_round_dir != expected_round_dir
            or resolved_manifest != resolved_round_dir / "round_manifest.json"
            or resolved_candidate
            != resolved_round_dir / "parameter_candidates_merged.json"
            or resolved_manifest.is_symlink()
            or resolved_candidate.is_symlink()
            or not isinstance(candidate_payload, dict)
        ):
            return False
        manifest_bytes = resolved_manifest.read_bytes()
        candidate_bytes = resolved_candidate.read_bytes()
        manifest_payload = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest_payload, dict):
            return False

        step2_round_id = step2_provenance.get("round_id")
        step2_candidate_sha256 = step2_provenance.get("candidate_sha256")
        if (
            not isinstance(step2_round_id, str)
            or _STEP2_ROUND_ID_RE.fullmatch(step2_round_id) is None
            or not isinstance(step2_candidate_sha256, str)
            or _SHA256_RE.fullmatch(step2_candidate_sha256) is None
        ):
            return False
        step2_snapshot = load_research_round_snapshot(
            round_id=step2_round_id,
            project_root=project_root,
            require_managed_db_truth=True,
        )
        step2_manifest = (
            step2_snapshot.get("manifest")
            if isinstance(step2_snapshot, dict)
            else None
        )
        step2_digests = (
            step2_manifest.get("artifact_sha256")
            if isinstance(step2_manifest, dict)
            else None
        )
        if (
            not isinstance(step2_snapshot, dict)
            or step2_snapshot.get("data_source") != "db"
            or step2_snapshot.get("phase") != ROUND_PHASE_STEP2
            or step2_snapshot.get("round_id") != step2_round_id
            or step2_snapshot.get("status") != "succeeded"
            or not isinstance(step2_digests, dict)
            or step2_digests.get("parameter_candidates.json")
            != step2_candidate_sha256
        ):
            return False
        step2_snapshot_sha256 = research_round_snapshot_fingerprint(
            step2_snapshot
        )
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        reread_candidate_payload = json.loads(candidate_bytes.decode("utf-8"))
        if (
            typed_json_sha256(candidate_payload)
            != typed_json_sha256(reread_candidate_payload)
            or manifest_payload.get("artifact_sha256", {}).get(
                candidate_path.name
            )
            != candidate_sha256
            or manifest_payload.get("artifact_size_bytes", {}).get(
                candidate_path.name
            )
            != len(candidate_bytes)
        ):
            return False

        return save_research_round_snapshot(
            round_id=round_id,
            phase=ROUND_PHASE_STEP3,
            status=round_status,
            round_path=str(resolved_round_dir),
            started_at=started_at,
            finished_at=finished_at,
            replay_only=False,
            manifest_payload=manifest_payload,
            summary_payload={
                "parameter_candidates_merged": candidate_payload,
            },
            conclusion_payload={
                "report_markdown_path": str(conclusion_path.resolve()),
            },
            artifacts_payload={
                "round_dir": str(resolved_round_dir),
                "manifest_path": str(resolved_manifest),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "manifest_size_bytes": len(manifest_bytes),
                "manifest_utf8": manifest_bytes.decode("utf-8"),
                "candidate_path": str(resolved_candidate),
                "candidate_sha256": candidate_sha256,
                "candidate_size_bytes": len(candidate_bytes),
                "candidate_utf8": candidate_bytes.decode("utf-8"),
                "step2_round_id": step2_round_id,
                "step2_candidate_sha256": step2_candidate_sha256,
                "step2_snapshot_sha256": step2_snapshot_sha256,
            },
        )
    except (
        DBUnavailableError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        log.warning(
            "Managed Step3 snapshot publication validation failed (%s)",
            type(exc).__name__,
        )
        return False


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
        help="Legacy name: validate schema before first batch; does not run DDL",
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
    parser.add_argument("--start", help="Override calibration start date (YYYY-MM-DD, UTC)")
    parser.add_argument("--end", help="Override calibration end date (YYYY-MM-DD, UTC)")
    parser.add_argument("--dataset-version", default="v1.0")
    args = parser.parse_args()
    args.dataset_version = _normalize_dataset_version(args.dataset_version)

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
                start=args.start,
                end=args.end,
                dataset_version=args.dataset_version,
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
        step2_baseline = _load_step2_baseline(
            step2_dir,
            expected_dataset_version=args.dataset_version,
            expected_symbol=_SYMBOL,
            expected_window={"start": args.start, "end": args.end},
        )
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
            dataset_version=args.dataset_version,
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

    conclusion_path = round_dir / "phase2_step3_research_conclusion.md"
    _build_step3_conclusion_report(
        calibration_results, all_rows,
        step3_recommendations, merged, constraint_result,
        step2_baseline, round_id,
        conclusion_path,
    )

    cal_ok = sum(
        1 for cr in calibration_results if cr["status"] == "succeeded"
    )
    cal_partial = sum(
        1 for cr in calibration_results if cr["status"] == "partial_success"
    )
    cal_fail = sum(
        1 for cr in calibration_results if cr["status"] == "failed"
    )
    # Manifest is written last and binds the exact importable candidate bytes.
    candidate_path = round_dir / "parameter_candidates_merged.json"
    artifact_sha256: dict[str, str] = {}
    artifact_size_bytes: dict[str, int] = {}
    candidate_scope_keys: tuple[str, ...] = ()
    candidate_payload: dict[str, Any] | None = None
    if candidate_path.is_file():
        candidate_bytes = candidate_path.read_bytes()
        candidate_payload = json.loads(candidate_bytes.decode("utf-8"))
        candidate_scope_keys = tuple(
            str(key) for key in candidate_payload.get("candidates", {})
        )
        artifact_sha256[candidate_path.name] = hashlib.sha256(
            candidate_bytes
        ).hexdigest()
        artifact_size_bytes[candidate_path.name] = len(candidate_bytes)
    round_status = _determine_step3_round_status(
        calibration_results=calibration_results,
        skip_calibration=args.skip_calibration,
        skip_merge=args.skip_merge,
        step2_baseline=step2_baseline,
        constraint_result=constraint_result,
        candidate_payload=candidate_payload,
    )
    step2_provenance = step2_baseline.get("_validated_provenance")
    if not isinstance(step2_provenance, dict):
        step2_provenance = {"status": "missing"}
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest_path = round_dir / "round_manifest.json"
    _write_manifest(
        calibration_results, [],  # Step 3 无 scan phase
        round_id, started_at, finished_at,
        manifest_path,
        extra_manifest={
            "schema_version": "aats.step3_round.v1",
            "phase": "step3",
            "status": round_status,
            "dataset_version": args.dataset_version,
            "scope": {
                "symbol": _SYMBOL,
                "families": sorted(
                    {
                        key.rsplit("_", 1)[0]
                        for key in candidate_scope_keys
                        if "_" in key
                    }
                ),
                "timeframes": sorted(
                    {
                        key.rsplit("_", 1)[1].lower()
                        for key in candidate_scope_keys
                        if "_" in key
                    }
                ),
                "combo_keys": sorted(candidate_scope_keys),
                "combo_count": len(candidate_scope_keys),
                "window": {"start": args.start, "end": args.end},
            },
            "input_refs": {
                "dataset_version": args.dataset_version,
                "window": {"start": args.start, "end": args.end},
                "step2": {
                    key: step2_provenance.get(key)
                    for key in (
                        "round_id",
                        "status",
                        "symbol",
                        "dataset_version",
                        "started_at",
                        "finished_at",
                        "candidate_sha256",
                        "window",
                    )
                },
            },
            "artifact_sha256": artifact_sha256,
            "artifact_size_bytes": artifact_size_bytes,
        },
        immutable=True,
    )

    if not _publish_managed_step3_snapshot(
        project_root=pathlib.Path(__file__).resolve().parent.parent,
        round_dir=round_dir,
        manifest_path=manifest_path,
        candidate_path=candidate_path,
        candidate_payload=candidate_payload,
        round_id=round_id,
        round_status=round_status,
        started_at=started_at,
        finished_at=finished_at,
        step2_provenance=step2_provenance,
        conclusion_path=conclusion_path,
    ):
        log.error(
            "Managed Step3 snapshot publication failed; refusing result marker "
            "and downstream import for round %s",
            round_id,
        )
        return 3

    round_result_payload = {
        "schema_version": "aats.step3_result.v1",
        "phase": "step3",
        "round_id": round_id,
        "round_dir": str(round_dir.resolve()),
        "candidate_path": str(candidate_path.resolve()),
        "candidate_sha256": artifact_sha256.get(candidate_path.name),
        "status": round_status,
        "symbol": _SYMBOL,
        "dataset_version": args.dataset_version,
        "window": {"start": args.start, "end": args.end},
        "step2_round_id": step2_provenance.get("round_id"),
        "step2_candidate_sha256": step2_provenance.get(
            "candidate_sha256"
        ),
    }
    immutable_json_write(round_result_payload, round_dir / "round_result.json")
    print(
        _STEP3_RESULT_PREFIX
        + json.dumps(
            round_result_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )

    # ================================================================
    # 最终汇总
    # ================================================================
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
    if round_status == "failed":
        return 3
    if round_status == "partial_success":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
