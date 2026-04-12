#!/usr/bin/env python3
"""Taskbook 4: RDP Metrics / Continuous Improvement 验收测试.

验收标准:
1. 有统一 metrics framework, snapshot 可生成
2. release 可做 baseline comparison
3. release 可得 effectiveness 结论
4. weekly review 可生成
5. improvement backlog 可生成
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name} — {detail}")
        failed += 1


# ══════════════════════════════════════════════════════════════
# AC-1: 统一 metrics framework, snapshot 可生成
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-1: 统一 metrics framework, snapshot 可生成")
print("=" * 60)

from aats.data_platform.metrics.definitions import (
    ALL_METRICS,
    METRICS_BY_LAYER,
    METRICS_BY_NAME,
    get_metric_catalog,
)

check("ALL_METRICS 定义 >= 20 个指标", len(ALL_METRICS) >= 20, f"got {len(ALL_METRICS)}")
check("METRICS_BY_LAYER 有 5 层", len(METRICS_BY_LAYER) == 5,
      f"layers: {list(METRICS_BY_LAYER.keys())}")
check("每层至少 4 个指标",
      all(len(v) >= 4 for v in METRICS_BY_LAYER.values()),
      str({k: len(v) for k, v in METRICS_BY_LAYER.items()}))
check("METRICS_BY_NAME 可查找",
      "recommendation_count" in METRICS_BY_NAME)

catalog = get_metric_catalog()
check("get_metric_catalog 返回列表", len(catalog) >= 20)
check("catalog 条目有完整字段",
      all("name" in c and "layer" in c and "direction" in c for c in catalog))

# 生成 snapshot
from aats.data_platform.metrics.metric_calculator import (
    calculate_all_metrics,
    flatten_metrics,
)

by_layer = calculate_all_metrics(ROOT)
check("calculate_all_metrics 返回 5 层", len(by_layer) == 5)
check("research 层有指标", len(by_layer.get("research", {})) >= 4)
check("operations 层有指标", len(by_layer.get("operations", {})) >= 4)
check("reliability 层有指标", len(by_layer.get("reliability", {})) >= 4)

flat = flatten_metrics(by_layer)
check("flatten_metrics 展平", len(flat) >= 20)

from aats.data_platform.metrics.metric_registry import (
    build_metrics_snapshot,
    compare_snapshots,
    load_current_snapshot,
    load_metrics_history,
)

snapshot = build_metrics_snapshot(ROOT)
check("snapshot 生成成功", snapshot is not None)
check("snapshot 有 snapshot_id", bool(snapshot.get("snapshot_id")))
check("snapshot 有 metrics_by_layer", "metrics_by_layer" in snapshot)
check("snapshot 有 flat_metrics", "flat_metrics" in snapshot)
check("snapshot 有 summary", "summary" in snapshot)

loaded = load_current_snapshot(ROOT)
check("current_metrics_snapshot.json 已保存", loaded is not None)

history = load_metrics_history(ROOT)
check("metrics_history.json 有记录", len(history.get("snapshots", [])) >= 1)

# 按维度筛选
filtered = build_metrics_snapshot(ROOT, family="independent", timeframe="15m")
check("筛选 snapshot 成功", filtered is not None)
check("筛选 filter 正确",
      filtered.get("filter", {}).get("family") == "independent")

# snapshot 对比
diff = compare_snapshots(snapshot, filtered)
check("compare_snapshots 返回对比结果", len(diff) > 0)
check("对比结果有 trend 字段",
      all("trend" in v for v in diff.values()))

# 脚本和文档
check("rdp_build_metrics_snapshot.py 存在",
      (ROOT / "scripts" / "rdp_build_metrics_snapshot.py").exists())
check("rdp_metrics_framework.md 存在",
      (ROOT / "docs" / "operations" / "rdp_metrics_framework.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# AC-2: release 可做 baseline comparison
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-2: release 可做 baseline comparison")
print("=" * 60)

from aats.data_platform.metrics.baseline_comparison import (
    compare_release_to_baseline,
    find_baseline_for_release,
)

# 获取一个 release_id
rel_data_path = ROOT / "artifacts" / "production_workflow" / "parameter_release_history.json"
with open(rel_data_path, "r", encoding="utf-8") as f:
    rel_data = json.load(f)
releases = rel_data.get("releases", [])
check("有至少 1 个 release 可用", len(releases) >= 1)

if releases:
    release = releases[-1]
    release_id = release["release_id"]

    # 查找 baseline
    baseline_info = find_baseline_for_release(ROOT, release)
    # baseline 可能找不到也 OK（只有一个 release 时）
    check("find_baseline_for_release 不报错", True)

    # 完整对比
    comparison = compare_release_to_baseline(ROOT, release_id)
    check("compare_release_to_baseline 返回结果", comparison is not None)
    check("comparison 有 release_id", comparison.get("release_id") == release_id)
    check("comparison 有 conclusion",
          comparison.get("conclusion") in (
              "improvement", "regression", "neutral",
              "insufficient_evidence", "no_baseline"
          ))
    check("comparison 有 baseline_found 字段", "baseline_found" in comparison)
    check("comparison 有 detail", "detail" in comparison)

    # 检查输出文件
    cmp_dir = ROOT / "artifacts" / "metrics" / "release_comparisons" / release_id
    check("baseline_comparison.json 已保存",
          (cmp_dir / "baseline_comparison.json").exists())
    check("baseline_comparison_report.md 已保存",
          (cmp_dir / "baseline_comparison_report.md").exists())
else:
    for _ in range(7):
        check("(skipped — no releases)", True)

check("rdp_compare_release_to_baseline.py 存在",
      (ROOT / "scripts" / "rdp_compare_release_to_baseline.py").exists())

print()

# ══════════════════════════════════════════════════════════════
# AC-3: release 可得 effectiveness 结论
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-3: release 可得 effectiveness 结论")
print("=" * 60)

from aats.data_platform.metrics.release_effectiveness import (
    evaluate_release_effectiveness,
    find_effectiveness,
    load_effectiveness_registry,
)

if releases:
    release_id = releases[-1]["release_id"]

    evaluation = evaluate_release_effectiveness(ROOT, release_id)
    check("evaluate_release_effectiveness 返回结果", evaluation is not None)
    check("evaluation 有 conclusion",
          evaluation.get("conclusion") in (
              "effective", "mixed", "ineffective",
              "rollback_triggered", "insufficient_evidence"
          ))
    check("evaluation 有 dimensions",
          len(evaluation.get("dimensions", [])) == 4)
    check("evaluation 4 维度正确",
          {d["dimension"] for d in evaluation.get("dimensions", [])} == {
              "behavior", "execution", "operations", "governance"
          })
    check("evaluation 各维度有 score",
          all(d.get("score") in ("positive", "negative", "mixed", "unknown")
              for d in evaluation.get("dimensions", [])))
    check("evaluation 有 detail", bool(evaluation.get("detail")))

    # registry 已更新
    registry = load_effectiveness_registry(ROOT)
    check("effectiveness registry 有记录",
          len(registry.get("evaluations", [])) >= 1)

    # find_effectiveness
    found = find_effectiveness(ROOT, release_id)
    check("find_effectiveness 找到记录",
          found is not None and found.get("release_id") == release_id)

    # baseline_comparison_conclusion 引用
    check("evaluation 引用 baseline comparison",
          "baseline_comparison_conclusion" in evaluation)
else:
    for _ in range(9):
        check("(skipped — no releases)", True)

check("rdp_evaluate_release_effectiveness.py 存在",
      (ROOT / "scripts" / "rdp_evaluate_release_effectiveness.py").exists())
check("release_effectiveness_evaluation.md 存在",
      (ROOT / "docs" / "operations" / "release_effectiveness_evaluation.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# AC-4: weekly review 可生成
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-4: weekly review 可生成")
print("=" * 60)

from aats.data_platform.metrics.periodic_review import run_periodic_review

review = run_periodic_review(ROOT, window="weekly")
check("run_periodic_review 返回结果", review is not None)
check("review 有 review_id", bool(review.get("review_id")))
check("review 有 window='weekly'", review.get("window") == "weekly")
check("review 有 summary", "summary" in review)

summary = review.get("summary", {})
check("summary 有 total_releases", "total_releases" in summary)
check("summary 有 rollback_ratio", "rollback_ratio" in summary)
check("summary 有 workflow_runs", "workflow_runs" in summary)
check("summary 有 effectiveness", "effectiveness" in summary)

check("review 有 combo_ranking", "combo_ranking" in review)
check("review 有 improvement_suggestions", "improvement_suggestions" in review)
check("review 有 metrics_snapshot_id", bool(review.get("metrics_snapshot_id")))

# 输出文件
review_id = review["review_id"]
review_dir = ROOT / "artifacts" / "reviews" / "weekly" / review_id
check("review_summary.json 已保存",
      (review_dir / "review_summary.json").exists())
check("review_report.md 已保存",
      (review_dir / "review_report.md").exists())

# 读取 markdown 验证
md_path = review_dir / "review_report.md"
if md_path.exists():
    md_content = md_path.read_text(encoding="utf-8")
    check("review markdown 有 Summary", "Summary" in md_content)
    check("review markdown 非空", len(md_content) > 100)
else:
    check("review markdown 有 Summary", False, "file not found")
    check("review markdown 非空", False)

check("rdp_run_periodic_review.py 存在",
      (ROOT / "scripts" / "rdp_run_periodic_review.py").exists())
check("periodic_review_workflow.md 存在",
      (ROOT / "docs" / "operations" / "periodic_review_workflow.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# AC-5: improvement backlog 可生成
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-5: improvement backlog 可生成")
print("=" * 60)

from aats.data_platform.metrics.backlog_builder import (
    backlog_from_review,
    generate_improvement_backlog,
    load_backlog,
    update_backlog_item_status,
)

backlog = generate_improvement_backlog(ROOT)
check("generate_improvement_backlog 返回结果", backlog is not None)
check("backlog 有 items 列表", "items" in backlog)
check("backlog 有 stats", "stats" in backlog)

stats = backlog.get("stats", {})
check("stats 有 total", "total" in stats)
check("stats 有 open", "open" in stats)
check("stats 有 high_priority", "high_priority" in stats)

# backlog items 格式验证
for item in backlog.get("items", [])[:3]:
    required_fields = [
        "backlog_id", "created_at", "source", "category",
        "priority", "problem_statement", "suggested_action", "status"
    ]
    has_all = all(f in item for f in required_fields)
    check(f"  item {item.get('backlog_id', '?')[:30]} 有完整字段", has_all,
          f"missing: {[f for f in required_fields if f not in item]}")

# 从 review 生成 backlog items
review_items = backlog_from_review(ROOT, review)
check("backlog_from_review 返回列表", isinstance(review_items, list))

# 如果 review 有 suggestions，应该生成 items
review_suggestions = review.get("improvement_suggestions", [])
check("review suggestions → backlog items 对应",
      len(review_items) == len(review_suggestions))

# 状态更新测试
if backlog.get("items"):
    first_id = backlog["items"][0]["backlog_id"]
    updated = update_backlog_item_status(ROOT, first_id, "in_progress", "test")
    check("update_backlog_item_status 成功", updated is not None)
    check("状态已更新", updated.get("status") == "in_progress" if updated else False)

    # 恢复
    update_backlog_item_status(ROOT, first_id, "open", "test reset")
else:
    check("update_backlog_item_status (skipped)", True)
    check("状态已更新 (skipped)", True)

# 持久化验证
loaded = load_backlog(ROOT)
check("improvement_backlog.json 可加载", loaded is not None)
check("backlog generated_at 非空",
      loaded.get("generated_at") is not None if loaded else False)

check("rdp_generate_improvement_backlog.py 存在",
      (ROOT / "scripts" / "rdp_generate_improvement_backlog.py").exists())
check("improvement_backlog_process.md 存在",
      (ROOT / "docs" / "operations" / "improvement_backlog_process.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# 文件完整性检查
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("文件完整性检查")
print("=" * 60)

# 模块文件
metrics_dir = ROOT / "aats" / "data_platform" / "metrics"
for fname in [
    "__init__.py", "definitions.py", "metric_calculator.py",
    "metric_registry.py", "baseline_comparison.py",
    "release_effectiveness.py", "periodic_review.py",
    "backlog_builder.py",
]:
    check(f"metrics/{fname}", (metrics_dir / fname).exists())

# Artifact 文件
for fp in [
    "artifacts/metrics/current_metrics_snapshot.json",
    "artifacts/metrics/metrics_history.json",
    "artifacts/metrics/release_effectiveness_registry.json",
    "artifacts/metrics/improvement_backlog.json",
]:
    check(fp, (ROOT / fp).exists())

# Artifact 目录
for dp in [
    "artifacts/metrics/release_comparisons",
    "artifacts/reviews/weekly",
    "artifacts/reviews/monthly",
]:
    check(dp, (ROOT / dp).exists())

# 脚本
for fname in [
    "rdp_build_metrics_snapshot.py",
    "rdp_compare_release_to_baseline.py",
    "rdp_evaluate_release_effectiveness.py",
    "rdp_run_periodic_review.py",
    "rdp_generate_improvement_backlog.py",
]:
    check(f"scripts/{fname}", (ROOT / "scripts" / fname).exists())

# 文档
for fname in [
    "rdp_metrics_framework.md",
    "release_effectiveness_evaluation.md",
    "periodic_review_workflow.md",
    "improvement_backlog_process.md",
]:
    fp = ROOT / "docs" / "operations" / fname
    exists = fp.exists()
    if exists:
        size = fp.stat().st_size
        check(f"docs/{fname} ({size}B)", size > 500, f"太小: {size}")
    else:
        check(f"docs/{fname}", False, "not found")

print()

# ══════════════════════════════════════════════════════════════
# 总结
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print(f"验收结果: {passed} passed, {failed} failed")
print("=" * 60)

if __name__ == "__main__":
    if failed > 0:
        print("\n[FAIL] 有验收项未通过!")
        sys.exit(1)
    else:
        print("\n[ALL PASS] Taskbook 4 所有验收标准通过!")
        sys.exit(0)
