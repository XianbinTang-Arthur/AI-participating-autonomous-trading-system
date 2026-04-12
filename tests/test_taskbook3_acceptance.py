#!/usr/bin/env python3
"""Taskbook 3: RDP Deployment/Scheduling/Reliability 验收测试.

验收标准:
1. ≥3 种 workflow 可通过 dispatcher 调度 (dry-run)
2. workflow 失败可记录并补跑
3. reliability check 生成 alert summary
4. dev/staging/prod 隔离明确
5. 长期运行 runbook 存在
"""

import json
import os
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
# 验收标准 1: ≥3 种 workflow 可调度
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-1: ≥3 种 workflow 可通过 dispatcher 调度")
print("=" * 60)

from aats.data_platform.operations.workflow_dispatcher import (
    list_available_workflows,
    load_workflow_config,
    run_workflow,
)

workflows = list_available_workflows(ROOT)
check("至少 3 个 workflow 配置", len(workflows) >= 3, f"found {len(workflows)}")

expected_wfs = {"data_maintenance", "research_cycle", "governance_cycle", "decision_cycle"}
found_wfs = set(workflows)
check("4 个预期 workflow 全部存在", expected_wfs.issubset(found_wfs),
      f"missing: {expected_wfs - found_wfs}")

# 每个 workflow 加载配置
for wf in expected_wfs:
    try:
        config = load_workflow_config(ROOT, wf)
        tasks = config.get("tasks", [])
        check(f"  {wf} 配置有效 ({len(tasks)} tasks)", len(tasks) >= 1)
    except Exception as e:
        check(f"  {wf} 配置有效", False, str(e))

# dry-run 一个 workflow
try:
    report = run_workflow(ROOT, "governance_cycle", dry_run=True, stop_on_failure=True)
    check("governance_cycle dry-run 成功",
          report.get("overall_status") in ("success", "partial"),
          f"status={report.get('overall_status')}")
    check("dry-run 生成 run_id", bool(report.get("run_id")))
    check("dry-run 有 tasks 报告", len(report.get("tasks", [])) >= 1)
except Exception as e:
    check("governance_cycle dry-run", False, str(e))

# 统一入口脚本存在
check("rdp_run_scheduled_workflow.py 存在",
      (ROOT / "scripts" / "rdp_run_scheduled_workflow.py").exists())

# 调度策略文档
check("rdp_scheduling_strategy.md 存在",
      (ROOT / "docs" / "operations" / "rdp_scheduling_strategy.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# 验收标准 2: workflow 失败可记录并补跑
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-2: workflow 失败可记录并补跑")
print("=" * 60)

from aats.data_platform.operations.failure_registry import (
    find_failure,
    list_open_failures,
    load_failures,
    record_failure,
    update_failure_status,
)
from aats.data_platform.operations.retry_manager import (
    auto_record_failures_from_report,
    retry_single_task,
)

# 记录一次失败
rec = record_failure(
    ROOT,
    workflow="governance_cycle",
    run_id="wf_test_20260404_000000",
    task_name="quality_monitor",
    error_message="test failure for acceptance",
    exit_code=1,
)
check("记录失败成功", rec is not None and "failure_id" in rec)
check("失败状态为 open", rec.get("status") == "open")

# 查找失败
found = find_failure(ROOT, rec["failure_id"])
check("按 ID 查找失败记录", found is not None)

# 列出 open 失败
open_list = list_open_failures(ROOT)
check("list_open_failures 包含新记录",
      any(f["failure_id"] == rec["failure_id"] for f in open_list))

# dry-run 补跑
retry_result = retry_single_task(ROOT, rec["failure_id"], dry_run=True)
check("补跑 dry-run 返回结果", retry_result is not None)
check("补跑 dry-run 标记 dry_run", retry_result.get("dry_run") is True)

# 清理: 标记为 resolved
update_failure_status(ROOT, rec["failure_id"], status="resolved", notes="test cleanup")
found_after = find_failure(ROOT, rec["failure_id"])
check("更新失败状态为 resolved", found_after.get("status") == "resolved")

# 脚本存在
check("rdp_record_workflow_failure.py 存在",
      (ROOT / "scripts" / "rdp_record_workflow_failure.py").exists())
check("rdp_retry_workflow_failure.py 存在",
      (ROOT / "scripts" / "rdp_retry_workflow_failure.py").exists())

# 失败恢复文档
check("workflow_failure_recovery.md 存在",
      (ROOT / "docs" / "operations" / "workflow_failure_recovery.md").exists())

# auto_record 测试
mock_report = {
    "workflow": "test_wf",
    "run_id": "wf_test_auto",
    "tasks": [
        {"name": "ok_task", "status": "success"},
        {"name": "bad_task", "status": "failed", "error": "auto test fail"},
    ],
}
auto_recs = auto_record_failures_from_report(ROOT, mock_report)
check("auto_record 从报告中提取失败", len(auto_recs) == 1)
check("auto_record 失败任务名正确",
      auto_recs[0].get("task_name") == "bad_task" if auto_recs else False)

# 清理 auto test 记录
if auto_recs:
    update_failure_status(ROOT, auto_recs[0]["failure_id"],
                         status="resolved", notes="auto test cleanup")

print()

# ══════════════════════════════════════════════════════════════
# 验收标准 3: reliability check 生成 alert summary
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-3: reliability check 生成 alert summary")
print("=" * 60)

from aats.data_platform.operations.reliability_checks import (
    DEFAULT_RELIABILITY_CHECKS,
    run_all_checks,
)
from aats.data_platform.operations.alerting import (
    build_alert_summary,
    format_alert_summary_text,
    load_current_alerts,
)

# 运行所有检查
results = run_all_checks(ROOT)
check("运行所有可靠性检查", len(results) >= 5, f"got {len(results)} checks")
check("检查结果有 passed 字段",
      all(hasattr(r, "passed") for r in results))

# 构建告警摘要
summary = build_alert_summary(ROOT, results)
check("生成告警摘要", summary is not None)
check("摘要包含 overall_status", "overall_status" in summary)
check("摘要包含 alerts 列表", "alerts" in summary)
check("摘要包含 check_results", "check_results" in summary)
check("摘要包含统计字段",
      all(k in summary for k in ["total_checks", "passed", "failed"]))

# current_alerts.json 已写入
alerts_data = load_current_alerts(ROOT)
check("current_alerts.json 已更新", alerts_data is not None)
check("current_alerts 有 generated_at",
      alerts_data.get("generated_at") is not None if alerts_data else False)

# 文本格式化
text = format_alert_summary_text(summary)
check("文本格式化输出非空", len(text) > 50)

# 检查 7 个默认检查项
check(f"默认 {len(DEFAULT_RELIABILITY_CHECKS)} 个检查项",
      len(DEFAULT_RELIABILITY_CHECKS) >= 7)

# 脚本存在
check("rdp_run_reliability_check.py 存在",
      (ROOT / "scripts" / "rdp_run_reliability_check.py").exists())
check("rdp_build_alert_summary.py 存在",
      (ROOT / "scripts" / "rdp_build_alert_summary.py").exists())

# 文档
check("reliability_alerting.md 存在",
      (ROOT / "docs" / "operations" / "reliability_alerting.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# 验收标准 4: dev/staging/prod 隔离明确
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-4: dev/staging/prod 隔离明确")
print("=" * 60)

from aats.data_platform.operations.environment_guard import (
    ENVIRONMENT_POLICIES,
    VALID_ENVIRONMENTS,
    get_current_environment,
    get_environment_info,
    get_observation_window_hours,
    get_policy,
    guard_direct_db_access,
    guard_parameter_apply,
    guard_parameter_rollback,
    guard_workflow_execution,
)

# 三个环境定义
check("3 个有效环境", len(VALID_ENVIRONMENTS) == 3)
check("环境包含 dev/staging/prod",
      set(VALID_ENVIRONMENTS) == {"dev", "staging", "prod"})

# 每个环境有策略
for env in VALID_ENVIRONMENTS:
    policy = get_policy(env)
    check(f"  {env} 策略存在", policy is not None)
    check(f"  {env} 有 description", "description" in policy)

# dev 最宽松
dev_policy = get_policy("dev")
check("dev 不需要 gate", dev_policy["require_gate_pass"] is False)
check("dev 不需要 approval", dev_policy["require_approval"] is False)
check("dev 观察窗口 0h", dev_policy["observation_window_hours"] == 0)

# staging 中等
staging_policy = get_policy("staging")
check("staging 需要 gate", staging_policy["require_gate_pass"] is True)
check("staging 不需要 approval", staging_policy["require_approval"] is False)
check("staging 观察窗口 24h", staging_policy["observation_window_hours"] == 24)

# prod 最严格
prod_policy = get_policy("prod")
check("prod 需要 gate", prod_policy["require_gate_pass"] is True)
check("prod 需要 approval", prod_policy["require_approval"] is True)
check("prod 禁止直接 DB", prod_policy["allow_direct_db_access"] is False)
check("prod 观察窗口 72h", prod_policy["observation_window_hours"] == 72)

# 守卫函数
apply_guard = guard_parameter_apply("prod")
check("prod apply guard 有条件",
      apply_guard.allowed and "conditions" in apply_guard.reason)

db_guard = guard_direct_db_access("prod")
check("prod 直接 DB guard 拒绝", db_guard.allowed is False)

wf_guard = guard_workflow_execution("governance_cycle", "dev")
check("dev workflow guard 允许", wf_guard.allowed is True)

# 环境信息
info = get_environment_info(ROOT)
check("get_environment_info 返回结果", info is not None)
check("环境信息包含 name", hasattr(info, "name"))

# 文档
check("environment_isolation_for_rdp.md 存在",
      (ROOT / "docs" / "operations" / "environment_isolation_for_rdp.md").exists())

print()

# ══════════════════════════════════════════════════════════════
# 验收标准 5: 长期运行 runbook 存在
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("AC-5: 长期运行 runbook 存在")
print("=" * 60)

docs_dir = ROOT / "docs" / "operations"

runbook_files = {
    "rdp_reliability_runbook.md": "可靠性运行手册",
    "rdp_workflow_calendar.md": "调度日历",
    "rdp_environment_matrix.md": "环境矩阵",
    "rdp_scheduling_strategy.md": "调度策略",
    "workflow_failure_recovery.md": "失败恢复指南",
    "reliability_alerting.md": "可靠性告警",
    "environment_isolation_for_rdp.md": "环境隔离",
}

for filename, desc in runbook_files.items():
    fp = docs_dir / filename
    exists = fp.exists()
    if exists:
        size = fp.stat().st_size
        check(f"{filename} ({desc})", size > 500,
              f"文件太小: {size} bytes")
    else:
        check(f"{filename} ({desc})", False, "file not found")

# 操作模块文件
ops_dir = ROOT / "aats" / "data_platform" / "operations"
ops_files = {
    "__init__.py": "operations 包",
    "workflow_dispatcher.py": "workflow 调度器",
    "failure_registry.py": "失败记录",
    "retry_manager.py": "补跑管理器",
    "reliability_checks.py": "可靠性检查",
    "alerting.py": "告警模块",
    "environment_guard.py": "环境守卫",
}

for filename, desc in ops_files.items():
    check(f"operations/{filename} ({desc})",
          (ops_dir / filename).exists())

# workflow 配置
configs_dir = ROOT / "configs" / "rdp_workflows"
for wf_name in expected_wfs:
    check(f"configs/{wf_name}.json",
          (configs_dir / f"{wf_name}.json").exists())

# scripts
scripts_dir = ROOT / "scripts"
script_files = [
    "rdp_run_scheduled_workflow.py",
    "rdp_record_workflow_failure.py",
    "rdp_retry_workflow_failure.py",
    "rdp_run_reliability_check.py",
    "rdp_build_alert_summary.py",
]
for sf in script_files:
    check(f"scripts/{sf}", (scripts_dir / sf).exists())

# artifacts
artifacts_checks = [
    "artifacts/operations/workflow_runs",
    "artifacts/operations/alerts",
    "artifacts/operations/workflow_failures.json",
    "artifacts/operations/alerts/current_alerts.json",
]
for ac in artifacts_checks:
    check(f"{ac} 存在", (ROOT / ac).exists())

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
        print("\n[ALL PASS] Taskbook 3 所有验收标准通过!")
        sys.exit(0)
