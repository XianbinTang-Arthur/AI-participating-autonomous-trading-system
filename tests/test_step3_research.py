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
    "scale_in_threshold 有默认值 0.60",
    merged["independent_15m"]["scale_in_threshold"]["value"] == 0.60,
)

print()

# ══════════════════════════════════════════════════════════════
# 9. P0 ��复验证: auto-fix 后 values 更新 (级联场景)
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("9. P0 修复: auto-fix 级联不使用过期 values")
print("=" * 60)

# 构造级联场景: close > entry 且 scale_in < entry
cascade = {
    "test_ft": {
        "entry_threshold": {"value": 0.30, "confidence": "high"},
        "close_threshold": {"value": 0.40, "confidence": "medium"},  # 违反 close <= entry
        "scale_in_threshold": {"value": 0.20, "confidence": "medium"},  # 违反 scale_in >= entry
        "de_risk_net_edge_bps": {"value": 5.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
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

check("约束规则数量 = 4", len(_s3._CONSTRAINT_RULES) == 4)

# 11a: 两者均为 None 时不报错
no_short = {
    "test_ft": {
        "entry_threshold": {"value": 0.40, "confidence": "high"},
        "close_threshold": {"value": 0.15, "confidence": "medium"},
        "scale_in_threshold": {"value": 0.60, "confidence": "medium"},
        "de_risk_net_edge_bps": {"value": 2.0, "confidence": "medium"},
        "failed_thesis_net_edge_bps": {"value": -1.0, "confidence": "low"},
    },
}
cr_no_short = _s3._validate_constraints(no_short)
check("无 short 参数: 全部通过", cr_no_short["all_passed"])

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

# 11c: 两者存在且违���
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
# 总结
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print(f"验证结果: {passed} passed, {failed} failed")
print("=" * 60)

if failed > 0:
    print("\n[FAIL] 有验证项未通过!")
    sys.exit(1)
else:
    print("\n[ALL PASS] Step 3 research 单元测试全部通过!")
    sys.exit(0)
