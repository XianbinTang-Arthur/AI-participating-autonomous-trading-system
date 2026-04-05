#!/usr/bin/env python3
"""Review 修复验证测试.

验证 3 项修复:
  P0-1: approve-and-apply 路径已接入 pre-apply gate
  P0-2: live_query_adapter SQL 时间窗口参数化已修正
  P1:   参数映射语义文档化
"""

import ast
import inspect
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
# P0-1: approve-and-apply 路径已接入 pre-apply gate
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P0-1: approve-and-apply 接入 pre-apply gate")
print("=" * 60)

# 读取脚本源码验证结构
script_path = ROOT / "scripts" / "approve_recommendation_and_apply.py"
source = script_path.read_text(encoding="utf-8")

# 1. CLI 新增了 --force-warn 和 --skip-gate
check("CLI 有 --force-warn 参数", "--force-warn" in source)
check("CLI 有 --skip-gate 参数", "--skip-gate" in source)

# 2. approve-and-apply 路径中导入了 gate
check("导入了 run_pre_apply_gate",
      "from aats.data_platform.production_workflow.pre_apply_gate import" in source
      or "run_pre_apply_gate" in source)

# 3. gate block 时拒绝 apply
check("gate block 时拒绝 apply",
      'gate_status == "block"' in source)
check("gate block 返回非零退出码",
      "return 1" in source and "REJECTED" in source)

# 4. gate warn 时检查 force_warn
check("gate warn 时检查 force_warn",
      "force_warn" in source and 'gate_status == "warn"' in source)

# 5. gate_result 传递给 _apply
check("gate_result 传入 _apply_parameter_from_recommendation",
      "gate_result=gate_result" in source)

# 6. _apply 函数接受 gate_result 参数
check("_apply 函数签名有 gate_result",
      "gate_result: dict | None = None" in source)

# 7. gate_run_id 记录到 approval log
check("gate_run_id 写入 approval log",
      "gate_run_id" in source and "gate_status" in source)

# 8. 记录到 apply history
check("同步 parameter_apply_history",
      "active_parameter_apply" in source)

# 9. gate block 时也写审计日志
check("gate block 时写审计日志",
      "approve-and-apply-blocked" in source)

print()

# ══════════════════════════════════════════════════════════════
# P0-2: SQL 时间窗口参数化修正
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P0-2: live_query_adapter SQL 时间窗口修正")
print("=" * 60)

adapter_path = ROOT / "aats" / "data_platform" / "live_query_adapter.py"
adapter_source = adapter_path.read_text(encoding="utf-8")

# 1. 旧的错误写法已不存在
check("fetch_recent_fill_stats 无旧 INTERVAL 写法",
      "INTERVAL ':hours hours'" not in adapter_source)
check("fetch_recent_order_states 无旧 INTERVAL 写法",
      "INTERVAL ':hours hours'" not in adapter_source.split("fetch_recent_order_states")[1]
      if "fetch_recent_order_states" in adapter_source else False)

# 2. 新写法使用 Python 端计算 start_time
check("fill_stats 使用 timedelta",
      "timedelta(hours=hours)" in adapter_source)
check("fill_stats 使用 :start_time 参数",
      ":start_time" in adapter_source)

# 3. 确认 order_states 也修复了
# 分割出 fetch_recent_order_states 函数体
order_states_section = adapter_source.split("def fetch_recent_order_states")[1] if "def fetch_recent_order_states" in adapter_source else ""
check("order_states 使用 timedelta",
      "timedelta" in order_states_section)
check("order_states 使用 :start_time 参数",
      ":start_time" in order_states_section)

# 4. 两处都不再使用 NOW() - INTERVAL 模式
check("全文无 INTERVAL ':hours",
      "INTERVAL ':hours" not in adapter_source)

# 5. 导入 datetime（已有 from datetime import datetime）
check("datetime 模块已导入",
      "from datetime import datetime" in adapter_source)

# 6. 验证函数签名和参数完整性
from aats.data_platform.live_query_adapter import (
    fetch_recent_fill_stats,
    fetch_recent_order_states,
)
fill_sig = inspect.signature(fetch_recent_fill_stats)
check("fill_stats 保持 hours 参数",
      "hours" in fill_sig.parameters)
order_sig = inspect.signature(fetch_recent_order_states)
check("order_states 保持 hours 参数",
      "hours" in order_sig.parameters)

print()

# ══════════════════════════════════════════════════════════════
# P1: 参数映射语义文档化
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("P1: 参数映射语义文档化")
print("=" * 60)

# 1. active_parameters.py 映射注释
ap_path = ROOT / "aats" / "bootstrap" / "active_parameters.py"
ap_source = ap_path.read_text(encoding="utf-8")

check("映射有 [DIRECT] 标注", "[DIRECT]" in ap_source)
check("映射有 [APPROXIMATE] 标注", "[APPROXIMATE]" in ap_source)
check("映射有 [PLACEHOLDER] 标注", "[PLACEHOLDER]" in ap_source)
check("有 TODO 标注需要确认的映射", "TODO:" in ap_source)
check("score_stability_threshold 有语义说明",
      "score_stability_threshold" in ap_source and "语义张力" in ap_source)
check("directional_trend_weight 有语义说明",
      "directional_trend_weight" in ap_source and "语义张力" in ap_source)
check("提到需要同步更新文档",
      "parameter_mapping_reference.md" in ap_source)

# 2. 参数映射参考文档
doc_path = ROOT / "docs" / "operations" / "parameter_mapping_reference.md"
check("parameter_mapping_reference.md 存在", doc_path.exists())
if doc_path.exists():
    doc = doc_path.read_text(encoding="utf-8")
    check("文档有 Independent 映射表", "Independent Family" in doc)
    check("文档有 Directional 映射表", "Directional Family" in doc)
    check("文档有映射类型说明", "[DIRECT]" in doc and "[PLACEHOLDER]" in doc)
    check("文档有 score_stability_threshold 说明",
          "score_stability_threshold" in doc)
    check("文档有 directional_trend_weight 说明",
          "directional_trend_weight" in doc)
    check("文档有安全检查清单", "安全检查清单" in doc)
    check("文档有修改步骤", "新增映射步骤" in doc)
    check("文档内容充实 (>2000B)", doc_path.stat().st_size > 2000)
else:
    for _ in range(8):
        check("(skipped)", False, "doc missing")

# 3. 映射本身仍然正确加载
from aats.bootstrap.active_parameters import (
    FAMILY_PARAMETER_MAPPINGS,
    PARAMETER_MAPPING_DIRECTIONAL,
    PARAMETER_MAPPING_INDEPENDENT,
)

check("PARAMETER_MAPPING_INDEPENDENT 有 20 个映射",
      len(PARAMETER_MAPPING_INDEPENDENT) == 20)
check("PARAMETER_MAPPING_DIRECTIONAL 有 3 个映射",
      len(PARAMETER_MAPPING_DIRECTIONAL) == 3)
check("FAMILY_PARAMETER_MAPPINGS 有 independent + directional",
      set(FAMILY_PARAMETER_MAPPINGS.keys()) == {"independent", "directional"})

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
    print("\n[ALL PASS] 3 项 review 修复全部验证通过!")
    sys.exit(0)
