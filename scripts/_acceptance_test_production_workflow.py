#!/usr/bin/env python3
"""RDP Production Workflow / Policy Gate 验收测试.

验收标准:
  1. recommendation approved 后，apply 前能跑 gate
  2. 每次 apply 都有 release record
  3. 每次 release 都有 observation summary
  4. 可生成 rollback recommendation
  5. 有 production parameter change runbook
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

errors = []
passed = []

# ===================================================================
# 准备: 确保有 approved recommendation 可用
# ===================================================================
print("=" * 60)
print("SETUP: 准备测试 recommendation")
print("=" * 60)

from aats.data_platform.decision_system.recommendation_registry import (
    add_recommendation,
    approve_recommendation,
    create_recommendation,
    find_recommendation,
    load_recommendation_registry,
    save_recommendation_registry,
)
from aats.data_platform.governance.parameter_registry import load_registry

gov_path = ROOT / "artifacts/governance/current_parameter_registry.json"
gov_reg = load_registry(gov_path)
frozen_sets = [ps for ps in gov_reg.get("parameter_sets", []) if ps["status"] == "frozen"]

if not frozen_sets:
    print("[SKIP] 无 frozen parameter set，部分测试将使用 candidate")
    candidate_sets = [ps for ps in gov_reg.get("parameter_sets", []) if ps["status"] in ("frozen", "candidate")]
    if candidate_sets:
        frozen_sets = candidate_sets

if not frozen_sets:
    print("[FATAL] 无可用 parameter set，无法运行验收测试")
    sys.exit(1)

target_ps = frozen_sets[0]
ps_id = target_ps["parameter_set_id"]
family = target_ps["family"]
timeframe = target_ps["timeframe"]

# 创建并 approve 一个 test recommendation
rec_path = ROOT / "artifacts/decision_system/recommendation_registry.json"
rec_reg = load_recommendation_registry(rec_path)

test_rec = create_recommendation(
    family=family,
    timeframe=timeframe,
    recommendation_type="parameter_upgrade",
    target_parameter_set_id=ps_id,
    confidence="medium",
    reason="production workflow acceptance test",
)
add_recommendation(rec_reg, test_rec)
approve_recommendation(rec_reg, test_rec["recommendation_id"], approved_by="acceptance_test")
save_recommendation_registry(rec_reg, rec_path)

rec_id = test_rec["recommendation_id"]
print(f"  [OK] Created test recommendation: {rec_id}")
print(f"  [OK] Target: {family}/{timeframe} -> {ps_id}")
print()

# ===================================================================
# Test 1: apply 前能跑 gate
# ===================================================================
print("=" * 60)
print("Test 1: apply 前能跑 gate")
print("=" * 60)

from aats.data_platform.production_workflow.pre_apply_gate import (
    run_pre_apply_gate,
)

gate_result = run_pre_apply_gate(ROOT, rec_id)

assert isinstance(gate_result, dict), "gate 应返回 dict"
assert "allow_apply" in gate_result, "gate 应包含 allow_apply"
assert "gate_status" in gate_result, "gate 应包含 gate_status"
assert "checks" in gate_result, "gate 应包含 checks"
assert "blocking_reasons" in gate_result, "gate 应包含 blocking_reasons"
assert "warnings" in gate_result, "gate 应包含 warnings"
assert gate_result["gate_status"] in ("pass", "warn", "block"), f"unknown status: {gate_result['gate_status']}"

print(f"  [OK] gate_status: {gate_result['gate_status']}")
print(f"  [OK] allow_apply: {gate_result['allow_apply']}")
print(f"  [OK] checks: {gate_result['passed_checks']}/{gate_result['total_checks']} passed")

for check in gate_result["checks"]:
    icon = "[PASS]" if check["passed"] else "[FAIL]"
    print(f"    {icon} {check['name']} ({check['severity']})")

# 验证 gate 结果保存
gate_run_id = gate_result["gate_run_id"]
gate_dir = ROOT / "artifacts/production_workflow/gates" / gate_run_id
assert gate_dir.exists(), "gate 结果目录应存在"
assert (gate_dir / "pre_apply_gate_result.json").exists(), "gate JSON 应存在"
assert (gate_dir / "pre_apply_gate_report.md").exists(), "gate report 应存在"
print(f"  [OK] gate 结果已保存: {gate_dir.name}")

passed.append("Test 1: apply 前能跑 gate")
print()

# ===================================================================
# Test 2: 每次 apply 都有 release record
# ===================================================================
print("=" * 60)
print("Test 2: 每次 apply 都有 release record")
print("=" * 60)

from aats.data_platform.production_workflow.release_registry import (
    create_parameter_release,
    find_release,
    load_release_history,
)

release_result = create_parameter_release(
    ROOT,
    recommendation_id=rec_id,
    actor="acceptance_test",
    observation_window_hours=24,
    notes="acceptance test release",
    run_gate=True,
    run_apply=True,
)

# 如果 gate blocked, 仍然应该有 release record
release = release_result.get("release", {})
assert "release_id" in release, "release 应有 release_id"
assert release.get("recommendation_id") == rec_id, "release 应关联 recommendation"
assert release.get("parameter_set_id") == ps_id, "release 应关联 parameter set"
print(f"  [OK] release_id: {release['release_id']}")
print(f"  [OK] apply_result: {release.get('apply_result')}")
print(f"  [OK] gate_status: {release.get('gate_status')}")

# 检查 release 在 history 中
history = load_release_history(ROOT)
found = find_release(history, release["release_id"])
assert found is not None, "release 应在 history 中"
print(f"  [OK] release 已保存到 history")

# 验证 release 字段完整性
required_fields = [
    "release_id", "created_at", "family", "timeframe",
    "recommendation_id", "parameter_set_id", "actor",
    "apply_result", "observation_status",
]
for field in required_fields:
    assert field in release, f"release 缺少字段: {field}"
print(f"  [OK] release 字段完整 ({len(required_fields)} fields)")

passed.append("Test 2: 每次 apply 都有 release record")
print()

# ===================================================================
# Test 3: 每次 release 都有 observation summary
# ===================================================================
print("=" * 60)
print("Test 3: 每次 release 都有 observation summary")
print("=" * 60)

from aats.data_platform.production_workflow.observation_window import (
    run_observation,
)

obs_result = run_observation(
    ROOT,
    release_id=release["release_id"],
    family=family,
    timeframe=timeframe,
    window_hours=24,
)

assert isinstance(obs_result, dict), "observation 应返回 dict"
assert "status" in obs_result, "observation 应包含 status"
assert "recommendation" in obs_result, "observation 应包含 recommendation"
assert "checklist" in obs_result, "observation 应包含 checklist"
assert obs_result["status"] in ("observing", "completed", "rollback_recommended")
assert obs_result["recommendation"] in ("keep", "review", "rollback_recommended")

print(f"  [OK] observation status: {obs_result['status']}")
print(f"  [OK] recommendation: {obs_result['recommendation']}")
print(f"  [OK] window_active: {obs_result['window_active']}")
print(f"  [OK] checklist: {len(obs_result['checklist'])} checks")

for check in obs_result["checklist"]:
    icon = {"ok": "[OK]", "warn": "[WARN]", "regression": "[REGR]", "unknown": "[?]"}.get(
        check.get("status", "?"), "[?]"
    )
    print(f"    {icon} {check['name']}: {check.get('detail', '')}")

# 验证文件保存
obs_dir = ROOT / "artifacts/production_workflow/observations" / release["release_id"]
assert obs_dir.exists(), "observation 目录应存在"
assert (obs_dir / "observation_summary.json").exists(), "observation JSON 应存在"
assert (obs_dir / "observation_report.md").exists(), "observation report 应存在"
print(f"  [OK] observation 结果已保存")

passed.append("Test 3: 每次 release 都有 observation summary")
print()

# ===================================================================
# Test 4: 可生成 rollback recommendation
# ===================================================================
print("=" * 60)
print("Test 4: 可生成 rollback recommendation")
print("=" * 60)

from aats.data_platform.production_workflow.rollback_policy import (
    evaluate_rollback_recommendation,
)

rb_result = evaluate_rollback_recommendation(
    ROOT,
    release_id=release["release_id"],
    family=family,
    timeframe=timeframe,
)

assert isinstance(rb_result, dict), "rollback eval 应返回 dict"
assert "rollback_recommended" in rb_result, "应包含 rollback_recommended"
assert "severity" in rb_result, "应包含 severity"
assert "reasons" in rb_result, "应包含 reasons"
assert "triggers" in rb_result, "应包含 triggers"
assert "suggested_target_parameter_set_id" in rb_result, "应包含 suggested target"
assert rb_result["severity"] in ("none", "medium", "high")

print(f"  [OK] rollback_recommended: {rb_result['rollback_recommended']}")
print(f"  [OK] severity: {rb_result['severity']}")
print(f"  [OK] triggers evaluated: {len(rb_result['triggers'])}")

for t in rb_result["triggers"]:
    icon = "[FIRED]" if t.get("fired") else "[OK]  "
    print(f"    {icon} {t['trigger']}: {t.get('detail', '')}")

if rb_result.get("reasons"):
    print(f"  [OK] reasons: {rb_result['reasons']}")
if rb_result.get("suggested_target_parameter_set_id"):
    print(f"  [OK] suggested target: {rb_result['suggested_target_parameter_set_id']}")

# 验证文件保存
rb_dir = ROOT / "artifacts/production_workflow/rollback_recommendations" / release["release_id"]
assert rb_dir.exists(), "rollback 目录应存在"
assert (rb_dir / "rollback_recommendation.json").exists(), "rollback JSON 应存在"
assert (rb_dir / "rollback_recommendation_report.md").exists(), "rollback report 应存在"
print(f"  [OK] rollback recommendation 结果已保存")

passed.append("Test 4: 可生成 rollback recommendation")
print()

# ===================================================================
# Test 5: 有 production parameter change runbook
# ===================================================================
print("=" * 60)
print("Test 5: 有 production parameter change runbook")
print("=" * 60)

runbook_path = ROOT / "docs/operations/production_parameter_change_runbook.md"
assert runbook_path.exists(), "runbook 应存在"
content = runbook_path.read_text(encoding="utf-8")

required_sections = [
    "Pre-Apply Gate",
    "Parameter Release",
    "Observation Window",
    "Rollback Recommendation",
]
for section in required_sections:
    assert section.lower() in content.lower(), f"runbook 缺少: {section}"
    print(f"  [OK] runbook has section: {section}")

line_count = len(content.splitlines())
assert line_count >= 50, f"runbook 太短: {line_count} lines"
print(f"  [OK] runbook has {line_count} lines")

passed.append("Test 5: 有 production parameter change runbook")
print()

# ===================================================================
# Bonus: API 路由完整性
# ===================================================================
print("=" * 60)
print("Bonus: API 路由完整性")
print("=" * 60)

from aats.api.rdp_routes import rdp_router

routes = []
for route in rdp_router.routes:
    path = getattr(route, "path", "")
    methods = getattr(route, "methods", set())
    routes.append((path, methods))

new_endpoints = [
    ("POST", "/rdp/gates/run"),
    ("POST", "/rdp/releases/create"),
    ("GET", "/rdp/releases/latest"),
    ("GET", "/rdp/releases/history"),
    ("POST", "/rdp/observations/run"),
    ("POST", "/rdp/rollback-recommendation/evaluate"),
]
for method, ep in new_endpoints:
    found = any(path == ep and method in methods for path, methods in routes)
    assert found, f"Missing {method} {ep}"
    print(f"  [OK] {method} {ep}")

print()

# Bonus: 脚本文件
print("=" * 60)
print("Bonus: 脚本文件完整性")
print("=" * 60)

for script in [
    "scripts/rdp_run_pre_apply_gate.py",
    "scripts/rdp_create_parameter_release.py",
    "scripts/rdp_run_post_apply_observation.py",
    "scripts/rdp_evaluate_rollback_recommendation.py",
]:
    spath = ROOT / script
    assert spath.exists(), f"{script} 应存在"
    content = spath.read_text(encoding="utf-8")
    assert "argparse" in content
    assert "__main__" in content
    print(f"  [OK] {script}")

print()

# Bonus: 模块完整性
print("=" * 60)
print("Bonus: production_workflow 模块")
print("=" * 60)

for module in [
    "aats/data_platform/production_workflow/__init__.py",
    "aats/data_platform/production_workflow/pre_apply_gate.py",
    "aats/data_platform/production_workflow/gate_rules.py",
    "aats/data_platform/production_workflow/release_registry.py",
    "aats/data_platform/production_workflow/observation_window.py",
    "aats/data_platform/production_workflow/rollback_policy.py",
]:
    mpath = ROOT / module
    assert mpath.exists(), f"{module} 应存在"
    print(f"  [OK] {module}")

print()

# ===================================================================
# SUMMARY
# ===================================================================
print("=" * 60)
print("ACCEPTANCE TEST SUMMARY")
print("=" * 60)
for p in passed:
    print(f"  PASS  {p}")
if errors:
    for e in errors:
        print(f"  FAIL  {e}")
    print(f"\n{len(passed)}/{len(passed)+len(errors)} passed")
    sys.exit(1)
else:
    print(f"\n{len(passed)}/{len(passed)} passed - ALL ACCEPTANCE CRITERIA MET")
    sys.exit(0)
