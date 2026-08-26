#!/usr/bin/env python3
"""Step 3 Research Orchestrator 单元测试.

验证:
  1. Step 2 基线加载 (_load_step2_baseline)
  2. 推荐合并 (_merge_recommendations)
  3. 约束校验 + 自动修复 (_validate_constraints)
  4. 合并参数候选输出 (_build_merged_parameter_candidates)
  5. CLI 参数解析 (argparse)
  6. 结论文档生成 (_build_step3_conclusion_report)
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 导入 step3 模块
_spec = importlib.util.spec_from_file_location(
    "_s3_test", ROOT / "scripts" / "rdp_run_step3_research.py",
)
_s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s3)

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
# 1. Step 2 基线加载
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("1. Step 2 基线加载")
print("=" * 60)

# 1a: 不存在时返回空结构
result = _s3._load_step2_baseline(
    step2_round_dir=Path("/nonexistent_path"),
    step2_artifact_root=Path("/also_nonexistent"),
)
check("不存在时返回空 candidates", result.get("candidates") == {})
check("不存在时返回空 pending", result.get("pending_validation") == [])

# 1b: 从临时目录加载
with tempfile.TemporaryDirectory() as tmpdir:
    tmppath = Path(tmpdir)
    s2_data = {
        "round_id": "test_20260401",
        "candidates": {
            "independent_1h": {
                "signal_edge_scale_bps": 12.0,
                "min_confirm_ticks": 3,
            },
        },
        "pending_validation": ["min_safe_net_edge_bps in independent_1h"],
    }
    (tmppath / "parameter_candidates.json").write_text(
        json.dumps(s2_data), encoding="utf-8",
    )
    loaded = _s3._load_step2_baseline(step2_round_dir=tmppath)
    check(
        "从临时目录加载成功",
        loaded.get("round_id") == "test_20260401",
    )
    check(
        "candidates 内容正确",
        loaded["candidates"]["independent_1h"]["signal_edge_scale_bps"] == 12.0,
    )

# 1c: 自动查找最新 round
with tempfile.TemporaryDirectory() as tmpdir:
    tmppath = Path(tmpdir)
    # 创建两个 round 目录
    (tmppath / "20260401_old").mkdir()
    (tmppath / "20260402_new").mkdir()
    old_data = {"round_id": "old", "candidates": {"x": {}}, "pending_validation": []}
    new_data = {"round_id": "new", "candidates": {"y": {}}, "pending_validation": []}
    (tmppath / "20260401_old" / "parameter_candidates.json").write_text(
        json.dumps(old_data), encoding="utf-8",
    )
    (tmppath / "20260402_new" / "parameter_candidates.json").write_text(
        json.dumps(new_data), encoding="utf-8",
    )
    auto = _s3._load_step2_baseline(step2_artifact_root=tmppath)
    check("自动选择最新 round", auto.get("round_id") == "new")

print()

# ══════════════════════════════════════════════════════════════
# 2. 推荐合并
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("2. 推荐合并")
print("=" * 60)

step2_baseline = {
    "candidates": {
        "independent_15m": {
            "signal_edge_scale_bps": 15.0,
            "min_confirm_ticks": 3,
            "taker_fee_bps": 5.0,
            "slippage_bps": 2.0,
        },
        "independent_1h": {
            "signal_edge_scale_bps": 12.0,
            "min_confirm_ticks": 2,
        },
    },
}

step3_recs = {
    "independent_15m_expanded": {
        "entry_threshold": {"value": 0.35, "confidence": "high", "reason": "test"},
        "close_threshold": {"value": 0.10, "confidence": "medium", "reason": "test"},
        "de_risk_net_edge_bps": {"value": 3.0, "confidence": "medium", "reason": "test"},
        "failed_thesis_net_edge_bps": {"value": -2.0, "confidence": "low", "reason": "test"},
        "min_hold_seconds": {"value": 600.0, "confidence": "medium", "reason": "test"},
        "rebalance_cooldown_seconds": {"value": 180.0, "confidence": "medium", "reason": "test"},
        "expected_slippage_buffer_bps": {"value": 1.0, "confidence": "high", "reason": "test"},
        "expected_execution_buffer_bps": {"value": 0.8, "confidence": "medium", "reason": "test"},
        "_cost_overall": {"confidence": "medium"},  # 应被忽略
    },
    "independent_1h_expanded": {
        "entry_threshold": {"value": 0.45, "confidence": "medium", "reason": "test"},
        "close_threshold": {"value": 0.20, "confidence": "low", "reason": "test"},
    },
}

merged = _s3._merge_recommendations(step2_baseline, step3_recs)

# 2a: 检查 ft_keys
check("合并后包含 independent_15m", "independent_15m" in merged)
check("合并后包含 independent_1h", "independent_1h" in merged)

# 2b: Step 2 基础参数保留
m15 = merged["independent_15m"]
check(
    "Step 2 base param 保留: signal_edge_scale_bps=15.0",
    m15["signal_edge_scale_bps"]["value"] == 15.0,
    f"got {m15.get('signal_edge_scale_bps', {}).get('value')}",
)
check(
    "Step 2 base param source=step2",
    m15["signal_edge_scale_bps"]["source"] == "step2",
)

# 2c: Step 3 扩展参数覆盖
check(
    "Step 3 expanded: entry_threshold=0.35",
    m15["entry_threshold"]["value"] == 0.35,
)
check(
    "Step 3 expanded source=step3",
    m15["entry_threshold"]["source"] == "step3",
)
check(
    "Step 3 expanded confidence=high",
    m15["entry_threshold"]["confidence"] == "high",
)

# 2d: 缺失参数用默认值填充
check(
    "缺失参数有默认值: min_safe_net_edge_bps",
    "min_safe_net_edge_bps" in m15,
)
check(
    "默认值 source=default",
    m15["min_safe_net_edge_bps"]["source"] == "default",
)

# 2e: _开头的 key 不应出现在合并结果中
check(
    "_cost_overall 不在合并结果中",
    "_cost_overall" not in m15,
)

# 2f: 1H 的合并
m1h = merged["independent_1h"]
check(
    "1H Step 3 entry_threshold=0.45",
    m1h["entry_threshold"]["value"] == 0.45,
)
check(
    "1H Step 2 保留 signal_edge_scale_bps=12.0",
    m1h["signal_edge_scale_bps"]["value"] == 12.0,
)

print()

# ══════════════════════════════════════════════════════════════
# 3. 约束校验 + 自动修复
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("3. 约束校验 + 自动修复")
print("=" * 60)

# 3a: 正常参数通过
ok_merged = {
    "independent_15m": {
        "entry_threshold": {"value": 0.40, "confidence": "high"},
        "close_threshold": {"value": 0.15, "confidence": "medium"},
        "scale_in_threshold": {"value": 0.60, "confidence": "medium"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
    },
}
cr = _s3._validate_constraints(ok_merged)
check("正常参数全部通过", cr["all_passed"])
check("无违反", len(cr["violations"]) == 0)
check("无修复", len(cr["auto_fixes"]) == 0)

# 3b: close > entry 违反
bad_close = {
    "independent_15m": {
        "entry_threshold": {"value": 0.30, "confidence": "high"},
        "close_threshold": {"value": 0.40, "confidence": "medium"},
        "scale_in_threshold": {"value": 0.60, "confidence": "medium"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
    },
}
cr_bad = _s3._validate_constraints(bad_close)
check("close > entry 检测到违反", not cr_bad["all_passed"])
check(
    "检测到 close <= entry 规则",
    any(v["rule"] == "close <= entry" for v in cr_bad["violations"]),
)
# 自动修复后 close 应该 = entry - 0.05
fixed_close = bad_close["independent_15m"]["close_threshold"]["value"]
check(
    f"自动修复 close: {fixed_close} == 0.25",
    fixed_close == 0.25,
    f"got {fixed_close}",
)

# 3c: failed_thesis > de_risk 违反
bad_ft = {
    "test_ft": {
        "de_risk_net_edge_bps": {"value": 1.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": 5.0, "confidence": "low"},
        "entry_threshold": {"value": 0.40, "confidence": "high"},
        "close_threshold": {"value": 0.15, "confidence": "medium"},
        "scale_in_threshold": {"value": 0.60, "confidence": "medium"},
    },
}
cr_ft = _s3._validate_constraints(bad_ft)
check(
    "failed_thesis > de_risk 检测到违反",
    any(v["rule"] == "failed_thesis <= de_risk" for v in cr_ft["violations"]),
)
# 自动修复: de_risk 上调至 failed_thesis + 3.0 = 8.0
fixed_dr = bad_ft["test_ft"]["de_risk_net_edge_bps"]["value"]
check(
    f"自动修复 de_risk: {fixed_dr} == 8.0",
    fixed_dr == 8.0,
    f"got {fixed_dr}",
)

# 3d: scale_in < entry 违反
bad_si = {
    "test_ft": {
        "entry_threshold": {"value": 0.50, "confidence": "high"},
        "close_threshold": {"value": 0.10, "confidence": "medium"},
        "scale_in_threshold": {"value": 0.30, "confidence": "medium"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
    },
}
cr_si = _s3._validate_constraints(bad_si)
check(
    "scale_in < entry 检测到违反",
    any(v["rule"] == "scale_in >= entry" for v in cr_si["violations"]),
)
# 自动修复: scale_in 上调至 entry + 0.10 = 0.60
fixed_si = bad_si["test_ft"]["scale_in_threshold"]["value"]
check(
    f"自动修复 scale_in: {fixed_si} == 0.60",
    fixed_si == 0.60,
    f"got {fixed_si}",
)

print()

# ══════════════════════════════════════════════════════════════
# 4. 合并参数候选输出
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("4. 合并参数候选输出")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmpdir:
    out_path = Path(tmpdir) / "test_candidates.json"
    constraint_ok = {"violations": [], "auto_fixes": [], "all_passed": True}

    _s3._build_merged_parameter_candidates(
        merged, constraint_ok, "test_round", out_path,
    )

    check("输出文件存在", out_path.exists())

    data = json.loads(out_path.read_text(encoding="utf-8"))
    check("round_id 正确", data["round_id"] == "test_round")
    check("scope.step=step3_merged", data["scope"]["step"] == "step3_merged")
    check("candidates 包含 independent_15m", "independent_15m" in data["candidates"])
    check("constraint_check.all_passed=True", data["constraint_check"]["all_passed"])

    # 验证值被正确提取
    c15m = data["candidates"]["independent_15m"]
    check(
        "signal_edge_scale_bps 值正确",
        c15m.get("signal_edge_scale_bps") == 15.0,
    )
    check(
        "entry_threshold 值正确",
        c15m.get("entry_threshold") == 0.35,
    )

print()

# ══════════════════════════════════════════════════════════════
# 5. 结论文档生成
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("5. 结论文档生成")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmpdir:
    out_path = Path(tmpdir) / "conclusion.md"
    cal_results = [
        {
            "round_key": "independent_15m_expanded",
            "family": "independent",
            "timeframe": "15m",
            "status": "succeeded",
            "batch_results": [
                {"_key": "entry_threshold", "status": "succeeded",
                 "summary": {"succeeded": 5, "failed": 0, "window": "2026-03-31..2026-04-02"}},
                {"_key": "close_threshold", "status": "succeeded",
                 "summary": {"succeeded": 4, "failed": 0}},
            ],
        },
    ]

    _s3._build_step3_conclusion_report(
        calibration_results=cal_results,
        all_rows=[],
        step3_recommendations=step3_recs,
        merged=merged,
        constraint_result={"violations": [], "auto_fixes": [], "all_passed": True},
        step2_baseline=step2_baseline,
        round_id="test_round",
        output_path=out_path,
    )

    check("结论文档存在", out_path.exists())
    content = out_path.read_text(encoding="utf-8")
    check("标题包含 Step 3", "Step 3" in content)
    check("包含 Scope 章节", "## 1. Scope" in content)
    check("包含 Merged 章节", "## 4. Merged" in content)
    check("包含 Constraint 章节", "## 5. Constraint" in content)
    check("包含 Next Steps 章节", "## 8. Next Steps" in content)
    check("包含 Phase 3 下一步", "Phase 3" in content)
    check("包含 Window 信息", "2026-03-31" in content)

print()

# ══════════════════════════════════════════════════════════════
# 6. CLI 参数解析
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("6. CLI 参数解析")
print("=" * 60)

# 验证 help 不报错
import subprocess
proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "rdp_run_step3_research.py"), "--help"],
    capture_output=True, text=True,
)
check("--help 正常退出", proc.returncode == 0)
check("help 输出包含 step3", "step 3" in proc.stdout.lower() or "step3" in proc.stdout.lower())
check("help 输出包含 --step2-round-dir", "--step2-round-dir" in proc.stdout)
check("help 输出包含 --skip-merge", "--skip-merge" in proc.stdout)
check("help 输出包含 --skip-calibration", "--skip-calibration" in proc.stdout)

print()

# ══════════════════════════════════════════════════════════════
# 7. 合并策略边界测试
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("7. 合并策略边界测试")
print("=" * 60)

# 7a: 空 Step 2 基线
empty_s2 = {"candidates": {}}
s3_only = {
    "independent_15m_expanded": {
        "entry_threshold": {"value": 0.35, "confidence": "high", "reason": "test"},
    },
}
m_empty = _s3._merge_recommendations(empty_s2, s3_only)
check("空 Step 2: 仍有 independent_15m", "independent_15m" in m_empty)
check(
    "空 Step 2: entry_threshold 来自 step3",
    m_empty["independent_15m"]["entry_threshold"]["source"] == "step3",
)
check(
    "空 Step 2: signal_edge_scale_bps 用默认值",
    m_empty["independent_15m"]["signal_edge_scale_bps"]["source"] == "default",
)

# 7b: 空 Step 3 推荐
s2_only = {
    "candidates": {
        "independent_15m": {
            "signal_edge_scale_bps": 10.0,
        },
    },
}
m_no_s3 = _s3._merge_recommendations(s2_only, {})
check("空 Step 3: 仍有 independent_15m", "independent_15m" in m_no_s3)
check(
    "空 Step 3: signal_edge_scale_bps 来自 step2",
    m_no_s3["independent_15m"]["signal_edge_scale_bps"]["source"] == "step2",
)

# 7c: None 值不应覆盖
s3_none = {
    "independent_15m_expanded": {
        "entry_threshold": {"value": None, "confidence": "high", "reason": "test"},
    },
}
m_none = _s3._merge_recommendations(s2_only, s3_none)
check(
    "None 值不覆盖默认值",
    m_none["independent_15m"]["entry_threshold"]["source"] == "default",
)

# ══════════════════════════════════════════════════════════════
# 8. P0 修复验证: scale_in_threshold 在合并集中
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("8. P0 修复: scale_in_threshold 合并")
print("=" * 60)

check(
    "scale_in_threshold 在 _STEP3_EXPANDED_PARAMS 中",
    "scale_in_threshold" in _s3._STEP3_EXPANDED_PARAMS,
)
# 合并结果应包含 scale_in_threshold
check(
    "合并结果包含 scale_in_threshold",
    "scale_in_threshold" in merged["independent_15m"],
)
check(
    # 2026-04-21 修正：truth source 对齐 replay_context.py L195 `scale_in_threshold=0.40`
    "scale_in_threshold 有默认值 0.40",
    merged["independent_15m"]["scale_in_threshold"]["value"] == 0.40,
)

print()

# ══════════════════════════════════════════════════════════════
# 9. P0 修复验证: auto-fix 后 values 更新 (级联场景)
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("9. P0 修复: auto-fix 级联不使用过期 values")
print("=" * 60)

# 构造级联场景: close > entry 且 scale_in < entry
# 注意: 需要给足 min_safe/slip/exe/max_thesis_age 以避免触发不相关的
# safe_edge > de_risk 与 min_hold <= max_thesis_age 约束
cascade = {
    "test_ft": {
        "entry_threshold": {"value": 0.30, "confidence": "high"},
        "close_threshold": {"value": 0.40, "confidence": "medium"},  # 违反 close <= entry
        "scale_in_threshold": {"value": 0.20, "confidence": "medium"},  # 违反 scale_in >= entry
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
        # 避免 safe_edge > de_risk 被触发: safe_edge = 4.0+0.5+0.5 = 5.0 >= 2.0+1.0
        "min_safe_net_edge_bps": {"value": 4.0, "confidence": "medium"},
        "expected_slippage_buffer_bps": {"value": 0.5, "confidence": "low"},
        "expected_execution_buffer_bps": {"value": 0.5, "confidence": "low"},
        # 避免 min_hold <= max_thesis_age 被触发
        "min_hold_seconds": {"value": 300.0, "confidence": "low"},
        "max_thesis_age_seconds": {"value": 1800.0, "confidence": "low"},
        # 避免 catastrophic_buffer >= 0 被触发
        "catastrophic_failed_thesis_buffer_bps": {"value": 3.0, "confidence": "low"},
    },
}
cr_cascade = _s3._validate_constraints(cascade)
check("级联: 检测到 2 个违反", len(cr_cascade["violations"]) == 2,
      f"got {len(cr_cascade['violations'])}")

# close 应被修复为 max(0, 0.30 - 0.05) = 0.25
fixed_close_c = cascade["test_ft"]["close_threshold"]["value"]
check(f"级联: close 修复为 {fixed_close_c} == 0.25", fixed_close_c == 0.25,
      f"got {fixed_close_c}")

# scale_in 应基于修复后的 entry (entry 没变, 还是 0.30)
# scale_in = entry + 0.10 = 0.40
fixed_si_c = cascade["test_ft"]["scale_in_threshold"]["value"]
check(f"级联: scale_in 修复为 {fixed_si_c} == 0.40", fixed_si_c == 0.40,
      f"got {fixed_si_c}")

print()

# ══════════════════════════════════════════════════════════════
# 10. P1 修复验证: 负 close_threshold 保护
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("10. P1 修复: close_threshold 负值保护")
print("=" * 60)

# entry=0.02 时, 不带 clamp 的话 close = -0.03, 应被 clamp 到 0.0
neg_case = {
    "test_ft": {
        "entry_threshold": {"value": 0.02, "confidence": "high"},
        "close_threshold": {"value": 0.10, "confidence": "medium"},  # > entry
        "scale_in_threshold": {"value": 0.60, "confidence": "medium"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
    },
}
_s3._validate_constraints(neg_case)
fixed_neg_close = neg_case["test_ft"]["close_threshold"]["value"]
check(f"close clamp: entry=0.02 → close={fixed_neg_close} >= 0",
      fixed_neg_close >= 0.0, f"got {fixed_neg_close}")
check(f"close clamp: close={fixed_neg_close} == 0.0",
      fixed_neg_close == 0.0, f"got {fixed_neg_close}")

print()

# ══════════════════════════════════════════════════════════════
# 11. P1 修复验证: short_close <= short_entry 约束
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("11. P1 修复: short_close <= short_entry 约束")
print("=" * 60)

# 约束规则总数 (独立 + 方向 + 短仓): failed_thesis<=de_risk, close<=entry,
# scale_in>=entry, short_close<=short_entry, safe_edge>de_risk,
# min_hold<=max_thesis_age, catastrophic_buffer>=0 → 共 7 条
check(
    "约束规则数量 = 7",
    len(_s3._CONSTRAINT_RULES) == 7,
    f"got {len(_s3._CONSTRAINT_RULES)}",
)
# P1-4+P1-5: 同样数量的 family-aware 规则
check(
    "independent 家族约束规则数量 = 7",
    len(_s3._get_constraint_rules("independent")) == 7,
)
check(
    "directional 家族约束规则数量 = 7",
    len(_s3._get_constraint_rules("directional")) == 7,
)

# 11a: 两者均为 None 时不报错
# 给足所有必要字段，避免 safe_edge/min_hold/catastrophic_buffer 触发
no_short = {
    "test_ft": {
        "entry_threshold": {"value": 0.40, "confidence": "high"},
        "close_threshold": {"value": 0.15, "confidence": "medium"},
        "scale_in_threshold": {"value": 0.60, "confidence": "medium"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
        "min_safe_net_edge_bps": {"value": 2.0, "confidence": "low"},
        "expected_slippage_buffer_bps": {"value": 0.5, "confidence": "low"},
        "expected_execution_buffer_bps": {"value": 0.5, "confidence": "low"},
        "min_hold_seconds": {"value": 300.0, "confidence": "low"},
        "max_thesis_age_seconds": {"value": 1800.0, "confidence": "low"},
        "catastrophic_failed_thesis_buffer_bps": {"value": 3.0, "confidence": "low"},
    },
}
cr_no_short = _s3._validate_constraints(no_short)
check("无 short 参数: 全部通过", cr_no_short["all_passed"],
      f"violations: {[v['rule'] for v in cr_no_short['violations']]}")

# 11b: 两者存在且合法
with_short_ok = {
    "test_ft": {
        **no_short["test_ft"],
        "short_entry_threshold": {"value": 0.40, "confidence": "medium"},
        "short_close_threshold": {"value": 0.20, "confidence": "medium"},
    },
}
cr_short_ok = _s3._validate_constraints(with_short_ok)
check("short_close=0.20 <= short_entry=0.40: 通过", cr_short_ok["all_passed"])

# 11c: 两者存在且违规
with_short_bad = {
    "test_ft": {
        **no_short["test_ft"],
        "short_entry_threshold": {"value": 0.20, "confidence": "medium"},
        "short_close_threshold": {"value": 0.40, "confidence": "medium"},
    },
}
cr_short_bad = _s3._validate_constraints(with_short_bad)
check(
    "short_close=0.40 > short_entry=0.20: 检测违反",
    any(v["rule"] == "short_close <= short_entry" for v in cr_short_bad["violations"]),
)

print()

# ══════════════════════════════════════════════════════════════
# 12. P2 修复验证: main() 返回 int
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("12. P2 修复: main() 返回 int")
print("=" * 60)

import inspect
sig = inspect.signature(_s3.main)
# from __future__ import annotations 将注解变为字符串 "int"
check("main() 返回类型注解 = int",
      sig.return_annotation in (int, "int"),
      f"got {sig.return_annotation!r}")

print()

# ══════════════════════════════════════════════════════════════
# 13. P3 修复验证: Step 3 base param 覆盖 Step 2 (高置信度)
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("13. P3 修复: Step3 覆盖 Step2 base param (高置信度)")
print("=" * 60)

# Step 3 对 base param signal_edge_scale_bps 给出 high confidence 推荐
# 应覆盖 Step 2 的 medium confidence
s2_for_override = {
    "candidates": {
        "independent_15m": {"signal_edge_scale_bps": 10.0},
    },
}
s3_override = {
    "independent_15m_expanded": {
        "signal_edge_scale_bps": {
            "value": 18.0, "confidence": "high", "reason": "step3 better",
        },
    },
}
m_override = _s3._merge_recommendations(s2_for_override, s3_override)
check(
    "high conf step3 覆盖 medium step2: value=18.0",
    m_override["independent_15m"]["signal_edge_scale_bps"]["value"] == 18.0,
)
check(
    "source=step3",
    m_override["independent_15m"]["signal_edge_scale_bps"]["source"] == "step3",
)

# 反向: Step 3 low confidence 不覆盖 Step 2 medium
s3_low = {
    "independent_15m_expanded": {
        "signal_edge_scale_bps": {
            "value": 5.0, "confidence": "low", "reason": "worse",
        },
    },
}
m_no_override = _s3._merge_recommendations(s2_for_override, s3_low)
check(
    "low conf step3 不覆盖 step2 base: value=10.0",
    m_no_override["independent_15m"]["signal_edge_scale_bps"]["value"] == 10.0,
)

print()

# ══════════════════════════════════════════════════════════════
# 14. P1-4+P1-5 修复: _PARAM_DEFAULTS 和 _CONSTRAINT_RULES family-aware
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("14. P1-4+P1-5: family-aware 默认值与约束规则")
print("=" * 60)

# 14a: _family_from_ft_key 正确解析
check(
    "independent_15m -> independent",
    _s3._family_from_ft_key("independent_15m") == "independent",
)
check(
    "directional_1h -> directional",
    _s3._family_from_ft_key("directional_1h") == "directional",
)
check(
    "directional_15m_expanded -> directional",
    _s3._family_from_ft_key("directional_15m_expanded") == "directional",
)
# 未知前缀回退到 independent
check(
    "unknown prefix -> independent (backward compat)",
    _s3._family_from_ft_key("mystery_5m") == "independent",
)

# 14b: _get_param_defaults 返回正确的 family-specific 默认
ind_defaults = _s3._get_param_defaults("independent")
dir_defaults = _s3._get_param_defaults("directional")
check(
    # 2026-04-21 修正：truth source 对齐 replay_context.py L188
    "independent entry_threshold = 0.30",
    ind_defaults["entry_threshold"] == 0.30,
)
check(
    "independent close_threshold = 0.15",
    ind_defaults["close_threshold"] == 0.15,
)
check(
    "directional entry_threshold = 0.45",
    dir_defaults["entry_threshold"] == 0.45,
)
check(
    "directional close_threshold = 0.20",
    dir_defaults["close_threshold"] == 0.20,
)
# 两者非 entry/close 字段应一致（仅 entry/close 差异）
check(
    "independent/directional 共享 de_risk=2.0",
    ind_defaults["de_risk_net_edge_bps"] == dir_defaults["de_risk_net_edge_bps"],
)
check(
    "independent/directional 共享 catastrophic_buffer=3.0",
    ind_defaults["catastrophic_failed_thesis_buffer_bps"]
    == dir_defaults["catastrophic_failed_thesis_buffer_bps"]
    == 3.0,
)

# 14c: _merge_recommendations 为 directional ft_key 回填 directional 默认值
s2_dir = {
    "candidates": {
        "directional_15m": {},  # 完全空，强制走默认值
    },
}
s3_empty: dict[str, dict] = {}
m_dir = _s3._merge_recommendations(s2_dir, s3_empty)
check(
    "directional 合并后 entry_threshold = 0.45 (directional 默认)",
    m_dir["directional_15m"]["entry_threshold"]["value"] == 0.45,
    f"got {m_dir['directional_15m']['entry_threshold']['value']}",
)
check(
    "directional 合并后 close_threshold = 0.20 (directional 默认)",
    m_dir["directional_15m"]["close_threshold"]["value"] == 0.20,
    f"got {m_dir['directional_15m']['close_threshold']['value']}",
)
# independent 对照
s2_ind = {
    "candidates": {
        "independent_15m": {},
    },
}
m_ind = _s3._merge_recommendations(s2_ind, s3_empty)
check(
    # 2026-04-21 修正：truth source 对齐 replay_context.py L188
    "independent 合并后 entry_threshold = 0.30 (independent 默认)",
    m_ind["independent_15m"]["entry_threshold"]["value"] == 0.30,
)
check(
    "independent 合并后 close_threshold = 0.15 (independent 默认)",
    m_ind["independent_15m"]["close_threshold"]["value"] == 0.15,
)

# 14d: _validate_constraints 为 directional 使用 directional fallback
# 如果只提供 close=0.99 而不提供 entry，auto-fix 应使用 directional
# 的 entry=0.45 作为 fallback → close = 0.45 - 0.05 = 0.40
dir_violation = {
    "directional_15m": {
        "close_threshold": {"value": 0.99, "confidence": "low"},
        # 其他字段充足以避免触发额外约束
        "min_safe_net_edge_bps": {"value": 2.0, "confidence": "low"},
        "expected_slippage_buffer_bps": {"value": 0.5, "confidence": "low"},
        "expected_execution_buffer_bps": {"value": 0.5, "confidence": "low"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "low"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
        "min_hold_seconds": {"value": 300.0, "confidence": "low"},
        "max_thesis_age_seconds": {"value": 1800.0, "confidence": "low"},
        "catastrophic_failed_thesis_buffer_bps": {"value": 3.0, "confidence": "low"},
    },
}
cr_dir = _s3._validate_constraints(dir_violation)
# 应检测到 close <= entry 违反 (entry fallback = 0.45, close = 0.99 > 0.45)
dir_close_rule_violated = any(
    v["rule"] == "close <= entry" for v in cr_dir["violations"]
)
check("directional: close > entry 违反被检测", dir_close_rule_violated)
# 违反记录应包含 family 字段
dir_violations_with_family = [
    v for v in cr_dir["violations"] if v.get("family") == "directional"
]
check(
    "directional: 违反记录带 family=directional",
    len(dir_violations_with_family) >= 1,
)
# auto-fix 应使用 directional 默认: new_close = 0.45 - 0.05 = 0.40
fixed_dir_close = dir_violation["directional_15m"]["close_threshold"]["value"]
check(
    f"directional auto-fix: close={fixed_dir_close} == 0.40 (directional entry=0.45 - 0.05)",
    abs(fixed_dir_close - 0.40) < 1e-9,
    f"got {fixed_dir_close}",
)

# 对照: independent 同样场景 auto-fix 应使用 independent 的 0.30
ind_violation = {
    "independent_15m": {
        "close_threshold": {"value": 0.99, "confidence": "low"},
        "min_safe_net_edge_bps": {"value": 2.0, "confidence": "low"},
        "expected_slippage_buffer_bps": {"value": 0.5, "confidence": "low"},
        "expected_execution_buffer_bps": {"value": 0.5, "confidence": "low"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "low"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
        "min_hold_seconds": {"value": 300.0, "confidence": "low"},
        "max_thesis_age_seconds": {"value": 1800.0, "confidence": "low"},
        "catastrophic_failed_thesis_buffer_bps": {"value": 3.0, "confidence": "low"},
    },
}
cr_ind = _s3._validate_constraints(ind_violation)
fixed_ind_close = ind_violation["independent_15m"]["close_threshold"]["value"]
check(
    # 2026-04-21 修正：independent entry=0.30，fix 后 close = 0.30 - 0.05 = 0.25
    f"independent auto-fix: close={fixed_ind_close} == 0.25 (independent entry=0.30 - 0.05)",
    abs(fixed_ind_close - 0.25) < 1e-9,
    f"got {fixed_ind_close}",
)

# 14e: directional 家族的 close_threshold 默认满足 close <= entry
# 即使完全空的 values, directional 的 0.20 <= 0.45 仍应通过
rules_dir = _s3._get_constraint_rules("directional")
close_rule = next(r for r in rules_dir if r["name"] == "close <= entry")
check("directional 空 values: close<=entry 默认通过", close_rule["check"]({}))

rules_ind = _s3._get_constraint_rules("independent")
close_rule_ind = next(r for r in rules_ind if r["name"] == "close <= entry")
check("independent 空 values: close<=entry 默认通过", close_rule_ind["check"]({}))

# 14f: 向后兼容别名 _PARAM_DEFAULTS 仍存在（内部值为 independent 默认）
check(
    "_PARAM_DEFAULTS 别名存在",
    hasattr(_s3, "_PARAM_DEFAULTS"),
)
check(
    # 2026-04-21 修正：_PARAM_DEFAULTS 等于 _INDEPENDENT_DEFAULTS，entry=0.30
    "_PARAM_DEFAULTS 内容 = independent 默认",
    _s3._PARAM_DEFAULTS["entry_threshold"] == 0.30,
)
check(
    "_CONSTRAINT_RULES 别名存在",
    hasattr(_s3, "_CONSTRAINT_RULES") and len(_s3._CONSTRAINT_RULES) == 7,
)

# 14g + 14h: 真相源一致性 — _DEFAULTS_BY_FAMILY 必须与
# ReplayParameterOverrides.for_family() / dataclass 默认完全一致。
# 如果 replay 端日后修改默认，本测试会立刻报错避免静默偏差。
try:
    from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides

    _replay_independent = ReplayParameterOverrides()  # 默认 = independent
    _replay_directional = ReplayParameterOverrides.for_family("directional")

    # 关键阈值字段必须逐一对齐（其他字段通过 14h 整体校验）
    # 2026-04-21 修正：取值同步到当前真源 replay_context.py L188-196 + L340-342：
    #   independent: entry=0.30, close=0.15, scale_in=0.40
    #   directional: entry=0.45, close=0.20, scale_in=0.55
    # 旧测试曾硬编码 entry=0.40 / scale_in=0.60，是 truth source 迁移前的残留，
    # 与 ReplayParameterOverrides 实际默认长期不一致但未被 CI 捕获（脚本未
    # 被 pytest 默认收集）。本次 P3-4 补齐 _DIRECTIONAL_DEFAULTS
    # scale_in=0.55 时顺手把测试对齐到真源。
    _critical_fields = [
        ("entry_threshold", 0.30, 0.45),
        ("close_threshold", 0.15, 0.20),
        ("scale_in_threshold", 0.40, 0.55),
        ("min_safe_net_edge_bps", 2.0, 2.0),
        ("de_risk_net_edge_bps", 2.0, 2.0),
        ("failed_thesis_net_edge_bps", -1.0, -1.0),
        ("catastrophic_failed_thesis_buffer_bps", 3.0, 3.0),
        ("min_hold_seconds", 300.0, 300.0),
        ("max_thesis_age_seconds", 1800.0, 1800.0),
        ("expected_slippage_buffer_bps", 0.5, 0.5),
        ("expected_execution_buffer_bps", 0.5, 0.5),
        ("signal_edge_scale_bps", 12.0, 12.0),
    ]

    _ind_d = _s3._get_param_defaults("independent")
    _dir_d = _s3._get_param_defaults("directional")

    for _field, _expected_ind, _expected_dir in _critical_fields:
        # Step 3 默认 = 期望值
        check(
            f"14g: _INDEPENDENT_DEFAULTS[{_field}] == {_expected_ind}",
            _ind_d.get(_field) == _expected_ind,
            f"got {_ind_d.get(_field)}",
        )
        check(
            f"14g: _DIRECTIONAL_DEFAULTS[{_field}] == {_expected_dir}",
            _dir_d.get(_field) == _expected_dir,
            f"got {_dir_d.get(_field)}",
        )
        # ReplayParameterOverrides 真相源 = 期望值（防止 replay 端漂移）
        check(
            f"14g: ReplayParameterOverrides().{_field} == {_expected_ind}",
            getattr(_replay_independent, _field) == _expected_ind,
            f"got {getattr(_replay_independent, _field)}",
        )
        check(
            f"14g: for_family('directional').{_field} == {_expected_dir}",
            getattr(_replay_directional, _field) == _expected_dir,
            f"got {getattr(_replay_directional, _field)}",
        )

    # 14h: directional 与 independent 的差异点 = {entry, close, scale_in}
    # （truth source 长期现状）。如果未来 for_family 增加新差异字段，
    # 此断言会报错并提醒同步更新 _DIRECTIONAL_DEFAULTS。
    _replay_diff_fields = {
        f
        for f in _critical_fields
        if getattr(_replay_independent, f[0]) != getattr(_replay_directional, f[0])
    }
    _expected_diff = {
        ("entry_threshold", 0.30, 0.45),
        ("close_threshold", 0.15, 0.20),
        ("scale_in_threshold", 0.40, 0.55),
    }
    check(
        "14h: ReplayParameterOverrides directional vs independent 差异 = {entry, close, scale_in}",
        _replay_diff_fields == _expected_diff,
        f"got diff fields = {_replay_diff_fields}",
    )

except ImportError as _e:
    check(
        "14g/14h: ReplayParameterOverrides 可导入",
        False,
        f"ImportError: {_e}",
    )

print()

# ══════════════════════════════════════════════════════════════
# 总结
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print(f"验证结果: {passed} passed, {failed} failed")
print("=" * 60)

if __name__ == "__main__":
    if failed > 0:
        print("\n[FAIL] 有验证项未通过!")
        sys.exit(1)
    else:
        print("\n[ALL PASS] Step 3 research 单元测试全部通过!")
        sys.exit(0)
