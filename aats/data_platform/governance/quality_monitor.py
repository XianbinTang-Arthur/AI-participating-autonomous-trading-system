"""质量监控与巡检.

对数据层、artifact 层、结果层进行基础自检。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

from .parameter_registry import load_registry

log = logging.getLogger(__name__)


# ── 检查项定义 ───────────────────────────────────────────────────────


CHECK_LEVELS = ("critical", "warning", "info")


def _make_check(
    category: str,
    name: str,
    level: str,
    passed: bool,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "category": category,
        "name": name,
        "level": level,
        "passed": passed,
        "detail": detail,
    }


# ── Artifact 层检查 ──────────────────────────────────────────────────


def check_artifact_integrity(project_root: pathlib.Path) -> list[dict[str, Any]]:
    """检查 artifact 目录完整性."""
    checks: list[dict[str, Any]] = []

    # 1. 关键目录是否存在
    expected_dirs = [
        "artifacts/research/experiments",
        "artifacts/research/calibration_batches",
        "artifacts/research/calibration_rounds",
        "artifacts/research/step2_rounds",
        "artifacts/research/step3_rounds",
        "artifacts/research/attribution_rounds",
        "artifacts/research/execution_rounds",
        "artifacts/governance",
    ]
    for d in expected_dirs:
        full_path = project_root / d
        checks.append(_make_check(
            "artifact", f"目录存在: {d}",
            "warning", full_path.exists(),
            f"路径: {full_path}",
        ))

    # 2. experiments 下是否有 artifact
    exp_root = project_root / "artifacts/research/experiments"
    if exp_root.exists():
        exp_count = sum(1 for d in exp_root.iterdir() if d.is_dir())
        checks.append(_make_check(
            "artifact", "experiments 非空",
            "info", exp_count > 0,
            f"实验数: {exp_count}",
        ))

        # 检查每个实验是否有 diagnostics.json
        missing_diag = 0
        for subdir in exp_root.iterdir():
            if subdir.is_dir():
                # 可能是参数扫描（有子目录）或单实验
                has_diag = (subdir / "diagnostics.json").exists()
                has_comp = (subdir / "comparison_summary.json").exists()
                if not has_diag and not has_comp:
                    # 检查子目录
                    sub_has = any(
                        (subdir / sub / "diagnostics.json").exists()
                        for sub in subdir.iterdir()
                        if (subdir / sub).is_dir()
                    ) if any(s.is_dir() for s in subdir.iterdir()) else False
                    if not sub_has:
                        missing_diag += 1

        checks.append(_make_check(
            "artifact", "experiments 均有 diagnostics",
            "warning", missing_diag == 0,
            f"缺少 diagnostics 的实验数: {missing_diag}",
        ))

    # 3. Round 目录 manifest 检查
    for phase_name, rel_path in [
        ("Phase 3 attribution", "artifacts/research/attribution_rounds"),
        ("Phase 4 execution", "artifacts/research/execution_rounds"),
    ]:
        root = project_root / rel_path
        if not root.exists():
            continue
        round_dirs = [d for d in root.iterdir() if d.is_dir()]
        if not round_dirs:
            continue

        missing_manifest = 0
        for rd in round_dirs:
            if not (rd / "round_manifest.json").exists():
                missing_manifest += 1

        checks.append(_make_check(
            "artifact", f"{phase_name} round manifest 完整",
            "critical", missing_manifest == 0,
            f"缺少 manifest 的 round 数: {missing_manifest}/{len(round_dirs)}",
        ))

    return checks


# ── 结果层检查 ────────────────────────────────────────────────────────


def check_result_quality(project_root: pathlib.Path) -> list[dict[str, Any]]:
    """检查研究结果质量."""
    checks: list[dict[str, Any]] = []

    # 检查 experiments 中的结果
    exp_root = project_root / "artifacts/research/experiments"
    if not exp_root.exists():
        return checks

    all_opening_zero = True
    all_edge_zero = True
    total_experiments = 0

    for subdir in exp_root.iterdir():
        if not subdir.is_dir():
            continue
        diag_file = subdir / "diagnostics.json"
        if not diag_file.exists():
            continue

        try:
            with diag_file.open(encoding="utf-8") as f:
                diag = json.load(f)
        except Exception:
            continue

        total_experiments += 1
        opening_count = diag.get("opening_count", 0)
        positive_edge_ratio = diag.get("positive_edge_ratio", 0)

        if opening_count > 0:
            all_opening_zero = False
        if positive_edge_ratio > 0:
            all_edge_zero = False

    if total_experiments > 0:
        checks.append(_make_check(
            "result", "至少有实验产生开仓信号",
            "critical", not all_opening_zero,
            f"实验总数: {total_experiments}, 全部 opening_count=0: {all_opening_zero}",
        ))
        checks.append(_make_check(
            "result", "至少有实验有正 edge",
            "warning", not all_edge_zero,
            f"实验总数: {total_experiments}, 全部 positive_edge_ratio=0: {all_edge_zero}",
        ))

    # 检查 round 结果
    for phase_name, rel_path in [
        ("Phase 3", "artifacts/research/attribution_rounds"),
        ("Phase 4", "artifacts/research/execution_rounds"),
    ]:
        root = project_root / rel_path
        if not root.exists():
            continue
        round_dirs = [d for d in root.iterdir() if d.is_dir()]
        if not round_dirs:
            continue

        all_failed = True
        for rd in round_dirs:
            mf = rd / "round_manifest.json"
            if not mf.exists():
                continue
            try:
                with mf.open(encoding="utf-8") as f:
                    manifest = json.load(f)
                combos = manifest.get("combos", [])
                has_success = any(
                    c.get("status") in ("succeeded", "partial_success")
                    for c in combos
                )
                if has_success:
                    all_failed = False
            except Exception:
                continue

        checks.append(_make_check(
            "result", f"{phase_name} 至少有成功的 round",
            "critical", not all_failed,
            f"round 数: {len(round_dirs)}, 全部失败: {all_failed}",
        ))

    return checks


# ── Parameter 文件检查 ────────────────────────────────────────────────


def check_parameter_files(project_root: pathlib.Path) -> list[dict[str, Any]]:
    """检查参数文件可解析性."""
    checks: list[dict[str, Any]] = []

    search_roots = [
        project_root / "artifacts/research/experiments",
        project_root / "artifacts/research/step2_rounds",
        project_root / "artifacts/research/step3_rounds",
    ]
    param_files: list[pathlib.Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        param_files.extend(root.rglob("parameter_recommendations.json"))
        param_files.extend(root.rglob("parameter_candidates.json"))
        param_files.extend(root.rglob("parameter_candidates_merged.json"))
        param_files.extend(root.rglob("replay_params_used.json"))

    unparseable = 0
    for pf in param_files:
        try:
            with pf.open(encoding="utf-8") as f:
                json.load(f)
        except Exception:
            unparseable += 1

    checks.append(_make_check(
        "parameter", "参数文件可解析",
        "warning", unparseable == 0,
        f"参数文件总数: {len(param_files)}, 不可解析: {unparseable}",
    ))

    # 检查 governance registry
    registry_path = project_root / "artifacts/governance/current_parameter_registry.json"
    if registry_path.exists():
        try:
            reg = load_registry(registry_path)
            ps_count = len(reg.get("parameter_sets", []))
            checks.append(_make_check(
                "parameter", "parameter registry 可解析",
                "critical", True,
                f"parameter sets 数: {ps_count}",
            ))
        except Exception as exc:
            checks.append(_make_check(
                "parameter", "parameter registry 可解析",
                "critical", False,
                f"解析错误: {exc}",
            ))

    return checks


# ── Governance 层检查 ────────────────────────────────────────────────


def check_governance_state(project_root: pathlib.Path) -> list[dict[str, Any]]:
    """检查治理层状态."""
    checks: list[dict[str, Any]] = []

    gov_root = project_root / "artifacts/governance"

    # 关键文件存在性
    expected_files = [
        "artifact_index.json",
        "active_round_index.json",
        "current_parameter_registry.json",
        "quality_monitor_summary.json",
    ]
    for fname in expected_files:
        fpath = gov_root / fname
        exists = fpath.exists()
        checks.append(_make_check(
            "governance", f"治理文件存在: {fname}",
            "warning" if fname == "quality_monitor_summary.json" else "info",
            exists,
            f"路径: {fpath}",
        ))

    # 文档存在性
    doc_root = project_root / "docs/operations"
    expected_docs = [
        "platform_runbook.md",
        "artifact_conventions.md",
        "parameter_governance.md",
        "round_lifecycle.md",
        "operator_checklist.md",
    ]
    for fname in expected_docs:
        fpath = doc_root / fname
        if fpath.exists():
            # 检查非空
            size = fpath.stat().st_size
            checks.append(_make_check(
                "governance", f"运营文档存在且非空: {fname}",
                "info", size > 100,
                f"文件大小: {size} bytes",
            ))
        else:
            checks.append(_make_check(
                "governance", f"运营文档存在: {fname}",
                "warning", False,
                f"路径: {fpath}",
            ))

    return checks


# ── 汇总 ─────────────────────────────────────────────────────────────


def run_quality_monitor(project_root: pathlib.Path) -> dict[str, Any]:
    """运行全部巡检，生成汇总."""
    all_checks: list[dict[str, Any]] = []

    log.info("巡检: artifact 完整性...")
    all_checks.extend(check_artifact_integrity(project_root))

    log.info("巡检: 结果质量...")
    all_checks.extend(check_result_quality(project_root))

    log.info("巡检: 参数文件...")
    all_checks.extend(check_parameter_files(project_root))

    log.info("巡检: 治理层状态...")
    all_checks.extend(check_governance_state(project_root))

    # 统计
    total = len(all_checks)
    passed = sum(1 for c in all_checks if c["passed"])
    failed = total - passed
    critical_fails = sum(
        1 for c in all_checks
        if not c["passed"] and c["level"] == "critical"
    )
    warning_fails = sum(
        1 for c in all_checks
        if not c["passed"] and c["level"] == "warning"
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "summary": {
            "total_checks": total,
            "passed": passed,
            "failed": failed,
            "critical_failures": critical_fails,
            "warning_failures": warning_fails,
            "health": (
                "healthy" if critical_fails == 0 and warning_fails == 0
                else "degraded" if critical_fails == 0
                else "unhealthy"
            ),
        },
        "checks": all_checks,
    }


# ── CLI 入口 ───────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Quality Monitor: 治理层质量巡检")
    parser.add_argument("--run", action="store_true", help="运行全部巡检")
    args = parser.parse_args()

    if not args.run:
        parser.print_help()
        sys.exit(2)

    _project_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
    result = run_quality_monitor(_project_root)

    from ._atomic_io import atomic_json_write

    gov_dir = _project_root / "artifacts" / "governance"
    gov_dir.mkdir(parents=True, exist_ok=True)
    out_path = gov_dir / "quality_monitor_summary.json"
    atomic_json_write(result, out_path)

    health = result["summary"]["health"]
    log.info(
        "Quality Monitor: %s (%d/%d passed)",
        health, result["summary"]["passed"], result["summary"]["total_checks"],
    )
    log.info("写入 -> %s", out_path)
    sys.exit(0)
