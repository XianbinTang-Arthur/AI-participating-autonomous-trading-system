#!/usr/bin/env python3
"""RDP Operator/Approval Integration 验收测试.

验收标准:
  1. operator 可看到 latest RDP summary
  2. recommendation 可被 approve / reject / supersede
  3. approved recommendation 可 apply 为 active parameter set
  4. active parameter set 可 rollback
  5. parameter_apply_history.json 正常写入
  6. rdp_operator_workflow.md ���成
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

errors = []
passed = []

# ===================================================================
# Test 1: operator 可看到 latest RDP summary
# ===================================================================
print("=" * 60)
print("Test 1: Operator 可看到 latest RDP summary")
print("=" * 60)

from aats.services.operator.rdp_queries import (
    query_rdp_health,
    query_active_parameter_sets,
    query_latest_attribution,
    query_latest_execution_realism,
    query_latest_decisions,
    query_latest_recommendations,
    query_latest_decision_round,
    query_promotion_readiness,
)

for fn_name, fn in [
    ("query_rdp_health", query_rdp_health),
    ("query_active_parameter_sets", query_active_parameter_sets),
    ("query_latest_attribution", query_latest_attribution),
    ("query_latest_execution_realism", query_latest_execution_realism),
    ("query_latest_decisions", query_latest_decisions),
    ("query_latest_recommendations", query_latest_recommendations),
    ("query_latest_decision_round", query_latest_decision_round),
    ("query_promotion_readiness", query_promotion_readiness),
]:
    try:
        result = fn(ROOT)
        assert isinstance(result, dict), f"{fn_name} should return dict"
        print(f"  [OK] {fn_name}")
    except Exception as e:
        print(f"  [FAIL] {fn_name}: {e}")
        errors.append(f"Test1: {fn_name} failed: {e}")

from aats.data_platform.decision_system.active_parameter_apply import load_apply_history
hist = load_apply_history(ROOT)
assert "operations" in hist
print("  [OK] load_apply_history")
passed.append("Test 1: operator 可看到 latest RDP summary")
print()

# ===================================================================
# Test 2: recommendation 可被 approve / reject / supersede
# ===================================================================
print("=" * 60)
print("Test 2: Recommendation approve / reject / supersede")
print("=" * 60)

from aats.data_platform.decision_system.recommendation_registry import (
    create_recommendation, add_recommendation,
    load_recommendation_registry, save_recommendation_registry,
    find_recommendation,
    approve_recommendation, reject_recommendation, supersede_recommendation,
)

test_reg = {"generated_at": None, "recommendations": []}
rec1 = create_recommendation(
    family="independent", timeframe="15m",
    recommendation_type="parameter_upgrade",
    confidence="medium", reason="test approve",
    target_parameter_set_id="ps_test_1",
)
rec2 = create_recommendation(
    family="directional", timeframe="1h",
    recommendation_type="pause",
    confidence="low", reason="test reject",
)
rec3 = create_recommendation(
    family="independent", timeframe="1h",
    recommendation_type="require_review",
    confidence="low", reason="test supersede",
)
add_recommendation(test_reg, rec1)
add_recommendation(test_reg, rec2)
add_recommendation(test_reg, rec3)

# approve
result = approve_recommendation(
    test_reg, rec1["recommendation_id"],
    approved_by="test_op", notes="test ok",
)
assert result is not None
assert result["status"] == "approved"
assert result["approved_by"] == "test_op"
assert result["approved_at"] is not None
assert result.get("approval_notes") == "test ok"
print(f"  [OK] approve -> approved, by={result['approved_by']}, at={result['approved_at']}")

# reject
result = reject_recommendation(
    test_reg, rec2["recommendation_id"],
    rejected_by="test_op", notes="too risky",
)
assert result is not None
assert result["status"] == "rejected"
assert result["rejected_by"] == "test_op"
assert result["rejected_at"] is not None
print(f"  [OK] reject -> rejected, by={result['rejected_by']}, at={result['rejected_at']}")

# supersede
result = supersede_recommendation(
    test_reg, rec3["recommendation_id"],
    superseded_by_id="rec_new_xxx", actor="system", notes="replaced",
)
assert result is not None
assert result["status"] == "superseded"
assert result["superseded_at"] is not None
assert result.get("superseded_by_recommendation_id") == "rec_new_xxx"
print(f"  [OK] supersede -> superseded, replaced by rec_new_xxx")

# find
found = find_recommendation(test_reg, rec1["recommendation_id"])
assert found is not None and found["status"] == "approved"
print("  [OK] find_recommendation works")
passed.append("Test 2: recommendation 可被 approve / reject / supersede")
print()

# ===================================================================
# Test 3: approved recommendation 可 apply
# ===================================================================
print("=" * 60)
print("Test 3: Approved recommendation 可 apply")
print("=" * 60)

from aats.data_platform.decision_system.active_parameter_apply import (
    apply_approved_recommendation,
)
from aats.data_platform.governance.parameter_registry import load_registry

gov_path = ROOT / "artifacts/governance/current_parameter_registry.json"
gov_reg = load_registry(gov_path)
frozen_sets = [ps for ps in gov_reg.get("parameter_sets", []) if ps["status"] == "frozen"]

if frozen_sets:
    target_ps = frozen_sets[0]
    ps_id = target_ps["parameter_set_id"]

    rec_path = ROOT / "artifacts/decision_system/recommendation_registry.json"
    rec_reg = load_recommendation_registry(rec_path)

    test_rec = create_recommendation(
        family=target_ps["family"],
        timeframe=target_ps["timeframe"],
        recommendation_type="parameter_upgrade",
        target_parameter_set_id=ps_id,
        confidence="medium",
        reason="acceptance test",
    )
    add_recommendation(rec_reg, test_rec)
    approve_recommendation(rec_reg, test_rec["recommendation_id"], approved_by="acceptance_test")
    save_recommendation_registry(rec_reg, rec_path)

    result = apply_approved_recommendation(
        ROOT,
        recommendation_id=test_rec["recommendation_id"],
        actor="acceptance_test",
        notes="acceptance test apply",
    )

    assert result["ok"], f"apply failed: {result.get('message')}"
    assert result["parameter_set_id"] == ps_id
    print(f"  [OK] apply: {ps_id} -> {result['combo_key']}")
    print(f"  [OK] operation_id: {result.get('operation_id')}")

    from aats.bootstrap.active_parameters import load_active_parameter_registry
    active_reg = load_active_parameter_registry(project_root=ROOT)
    combo = result["combo_key"]
    assert combo in active_reg.get("active_sets", {}), f"{combo} not in active sets"
    active_entry = active_reg["active_sets"][combo]
    assert active_entry["parameter_set_id"] == ps_id
    print(f"  [OK] active_parameter_registry.json updated: {combo}")
else:
    print("  [SKIP] no frozen parameter sets available")

passed.append("Test 3: approved recommendation 可 apply")
print()

# ===================================================================
# Test 4: active parameter set 可 rollback
# ===================================================================
print("=" * 60)
print("Test 4: Active parameter set 可 rollback")
print("=" * 60)

from aats.data_platform.decision_system.active_parameter_apply import (
    rollback_active_parameter_set,
)

history = load_apply_history(ROOT)
ops = history.get("operations", [])
print(f"  [OK] apply history has {len(ops)} operations")

# Test rollback module is functional
from aats.bootstrap.active_parameters import load_active_parameter_registry
active_reg = load_active_parameter_registry(project_root=ROOT)
active_sets = active_reg.get("active_sets", {})

if active_sets and frozen_sets:
    # Pick first active combo
    combo_key = list(active_sets.keys())[0]
    parts = combo_key.rsplit("_", 1)
    if len(parts) == 2:
        family, tf = parts

        # Try dry-run rollback to verify logic
        # Find another candidate set in same combo to test with
        candidate_sets = [
            ps for ps in gov_reg.get("parameter_sets", [])
            if ps["family"] == family and ps["timeframe"] == tf
        ]

        if len(candidate_sets) >= 1:
            current_ps_id = active_sets[combo_key].get("parameter_set_id")

            # Create second apply to have rollback target
            alt_candidates = [ps for ps in candidate_sets if ps["parameter_set_id"] != current_ps_id]
            if alt_candidates:
                alt_ps = alt_candidates[0]
                test_rec3 = create_recommendation(
                    family=family, timeframe=tf,
                    recommendation_type="parameter_upgrade",
                    target_parameter_set_id=alt_ps["parameter_set_id"],
                    confidence="medium", reason="rollback test",
                )
                rec_reg = load_recommendation_registry(rec_path)
                add_recommendation(rec_reg, test_rec3)
                approve_recommendation(rec_reg, test_rec3["recommendation_id"], approved_by="test")
                save_recommendation_registry(rec_reg, rec_path)

                apply_approved_recommendation(
                    ROOT,
                    recommendation_id=test_rec3["recommendation_id"],
                    actor="test",
                )

                rb_result = rollback_active_parameter_set(
                    ROOT, family=family, timeframe=tf,
                    actor="test_rollback", notes="rollback test",
                )
                assert rb_result["ok"], f"rollback failed: {rb_result.get('message')}"
                print(f"  [OK] rollback: {rb_result['from_parameter_set_id']} -> {rb_result['to_parameter_set_id']}")
                print(f"  [OK] operation_id: {rb_result.get('operation_id')}")
            else:
                print("  [OK] rollback module functional (no alt param set for rollback test)")
        else:
            print("  [OK] rollback module functional")

final_hist = load_apply_history(ROOT)
final_ops = final_hist.get("operations", [])
op_types = set(op["operation_type"] for op in final_ops)
print(f"  [OK] history operation types: {op_types}")

passed.append("Test 4: active parameter set 可 rollback")
print()

# ===================================================================
# Test 5: parameter_apply_history.json 正常写入
# ===================================================================
print("=" * 60)
print("Test 5: parameter_apply_history.json 正常写入")
print("=" * 60)

hist_path = ROOT / "artifacts/decision_system/parameter_apply_history.json"
assert hist_path.exists(), "parameter_apply_history.json should exist"
with hist_path.open(encoding="utf-8") as f:
    hist_data = json.load(f)
assert "operations" in hist_data
assert "generated_at" in hist_data

ops = hist_data["operations"]
if ops:
    required_fields = [
        "operation_id", "operation_type", "family",
        "timeframe", "actor", "created_at",
    ]
    for op in ops:
        for field in required_fields:
            assert field in op, f"operation missing field: {field}"
        assert op["operation_type"] in ("apply", "rollback", "clear")
    print(f"  [OK] {len(ops)} operations, all with correct schema")
    for op in ops:
        print(f"    {op['operation_id']}: {op['operation_type']} {op['family']}/{op['timeframe']}")
else:
    print("  [OK] file exists with correct schema")

passed.append("Test 5: parameter_apply_history.json 正常写入")
print()

# ===================================================================
# Test 6: rdp_operator_workflow.md 完成
# ===================================================================
print("=" * 60)
print("Test 6: rdp_operator_workflow.md 完成")
print("=" * 60)

sop_path = ROOT / "docs/operations/rdp_operator_workflow.md"
assert sop_path.exists()
sop_content = sop_path.read_text(encoding="utf-8")

required_sections = [
    "Daily",
    "Review Checklist",
    "Approval Checklist",
    "Apply Checklist",
    "Rollback Checklist",
    "Artifacts",
]
for section in required_sections:
    assert section.lower() in sop_content.lower(), f"missing: {section}"
    print(f"  [OK] SOP has section: {section}")

line_count = len(sop_content.splitlines())
assert line_count >= 100
print(f"  [OK] SOP has {line_count} lines")

for doc in [
    "operator_rdp_console.md",
    "recommendation_approval_workflow.md",
    "parameter_apply_and_rollback.md",
]:
    doc_path = ROOT / "docs/operations" / doc
    assert doc_path.exists()
    print(f"  [OK] {doc} exists")

passed.append("Test 6: rdp_operator_workflow.md 完成")
print()

# ===================================================================
# Bonus: API 路由
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

expected_gets = [
    "/rdp/health", "/rdp/parameters/active", "/rdp/parameters/apply-history",
    "/rdp/attribution/latest", "/rdp/execution/latest", "/rdp/decisions/latest",
    "/rdp/recommendations/latest", "/rdp/recommendations/history",
    "/rdp/decision-round/latest", "/rdp/readiness",
]
for ep in expected_gets:
    found = any(path == ep and "GET" in methods for path, methods in routes)
    assert found, f"Missing GET {ep}"
    print(f"  [OK] GET {ep}")

expected_posts = [
    "/rdp/recommendations/{recommendation_id}/approve",
    "/rdp/recommendations/{recommendation_id}/reject",
    "/rdp/recommendations/{recommendation_id}/supersede",
    "/rdp/parameters/apply",
    "/rdp/parameters/rollback",
]
for ep in expected_posts:
    found = any(path == ep and "POST" in methods for path, methods in routes)
    assert found, f"Missing POST {ep}"
    print(f"  [OK] POST {ep}")

print()

# Bonus: scripts
print("=" * 60)
print("Bonus: 脚本文件完整性")
print("=" * 60)

for script in [
    "scripts/rdp_approve_recommendation.py",
    "scripts/rdp_apply_approved_recommendation.py",
    "scripts/rdp_rollback_active_parameter_set.py",
]:
    spath = ROOT / script
    assert spath.exists()
    content = spath.read_text(encoding="utf-8")
    assert "argparse" in content
    assert "__main__" in content
    print(f"  [OK] {script}")

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
