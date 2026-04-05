#!/usr/bin/env python3
"""显示当前配置的参数来源追踪.

离线诊断工具 — 不启动交易系统，仅模拟完整配置合并流程，
输出每个策略参数的最终值和来源层级。

用法::

    python scripts/show_settings_provenance.py
    python scripts/show_settings_provenance.py --field strategy_signal_edge_scale_bps
    python scripts/show_settings_provenance.py --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
os.chdir(_project_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="参数来源追踪诊断")
    parser.add_argument(
        "--field", "-f",
        help="查询单个字段的完整变更链",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式报告",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="显示所有追踪字段（包括未被覆盖的）",
    )
    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )

    from aats.bootstrap.settings import AATSSettings
    from aats.bootstrap.settings_provenance import SettingsProvenanceTracker

    tracker = SettingsProvenanceTracker()

    # ── Layer 1: Hardcoded defaults ──────────────────────────────
    defaults = AATSSettings.model_validate({}).model_dump(mode="python")
    tracker.snapshot("hardcoded_defaults", defaults)

    # ── Layer 2+3: YAML/Managed + Env → load_settings ───────────
    from aats.bootstrap.config import load_settings, _load_settings_layers
    base_settings = load_settings()

    if _load_settings_layers.get("yaml_or_managed"):
        yaml_merged = {**defaults, **_load_settings_layers["yaml_or_managed"]}
        tracker.snapshot("strategy_profile", yaml_merged)
        if _load_settings_layers.get("env_overrides"):
            env_merged = {**yaml_merged, **_load_settings_layers["env_overrides"]}
            tracker.snapshot("env_overrides", env_merged)

    base_dict = base_settings.model_dump(mode="python")
    tracker.snapshot("load_settings", base_dict)

    # ── Layer 4: Profile resolution ──────────────────────────────
    from aats.services.operator.runtime_profiles import runtime_profile_resolution
    profile_resolution = runtime_profile_resolution(settings=base_settings)
    tracker.snapshot("profile_resolution", profile_resolution.resolved_settings)

    # ── Layer 5: Active Parameters ───────────────────────────────
    from aats.bootstrap.active_parameters import apply_active_parameters_to_settings
    resolved = profile_resolution.resolved_settings
    enabled = resolved.get("active_parameters_enabled", False)
    if enabled:
        resolved = apply_active_parameters_to_settings(
            resolved,
            project_root=Path.cwd(),
        )
        tracker.snapshot("active_parameters", resolved)
    else:
        print("\n[!] active_parameters_enabled = false — RDP 参数未注入\n")

    # ── Layer 6: Final validated ─────────────────────────────────
    final_settings = AATSSettings.model_validate(resolved)
    tracker.snapshot("final", final_settings.model_dump(mode="python"))

    # ── 输出 ─────────────────────────────────────────────────────
    if args.json:
        report = tracker.build_report()
        print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
        return

    if args.field:
        _show_single_field(tracker, args.field)
        return

    print()
    tracker.log_report()
    print()
    tracker.log_active_parameter_details()
    print()

    report = tracker.build_report()
    source_info = _load_settings_layers.get("source_type", {})
    print(f"--- 配置来源 ---")
    print(f"  env_template_profile: {source_info.get('env_template_profile', 'N/A')}")
    print(f"  environment: {source_info.get('environment', 'N/A')}")
    print(f"  config_profile: {source_info.get('config_profile', 'N/A')}")
    print(f"  active_parameters_enabled: {enabled}")
    print()
    print(f"--- 统计 ---")
    print(f"  总追踪策略字段: {report['total_tracked_fields']}")
    print(f"  被覆盖字段数: {report['overridden_field_count']}")
    print(f"  RDP 注入字段数: {report['active_parameter_field_count']}")

    if args.all:
        from aats.bootstrap.settings_provenance import _format_value
        print()
        print("--- 所有追踪字段 ---")
        all_fields = tracker.get_all_tracked_fields()
        for field_name in sorted(all_fields):
            prov = all_fields[field_name]
            print(f"  {field_name} = {_format_value(prov.final_value)}  (from {prov.final_layer})")


def _show_single_field(tracker: "SettingsProvenanceTracker", field_name: str) -> None:
    """显示单个字段的完整变更链."""
    from aats.bootstrap.settings_provenance import _format_value

    prov = tracker.get_field_provenance(field_name)
    if prov is None:
        # 尝试模糊匹配
        candidates = tracker.find_fields(field_name)
        if candidates:
            print(f"\n未找到精确匹配 '{field_name}'，相似字段:")
            for p in candidates[:10]:
                print(f"  {p.field_name} = {_format_value(p.final_value)}")
        else:
            print(f"\n未找到字段 '{field_name}'（可能不在追踪范围内）")
        return

    print(f"\n字段: {field_name}")
    print(f"最终值: {_format_value(prov.final_value)}")
    print(f"最终来源: {tracker.LAYER_LABELS.get(prov.final_layer, prov.final_layer)}")
    print(f"被覆盖: {'是' if prov.was_overridden else '否'}")
    print(f"\n变更历史:")
    for i, (layer, val) in enumerate(prov.history):
        label = tracker.LAYER_LABELS.get(layer, layer)
        marker = "  " if i < len(prov.history) - 1 else "→ "
        print(f"  {marker}{label}: {_format_value(val)}")


if __name__ == "__main__":
    main()
