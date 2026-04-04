# Recommendation → Production 工作流

## 1. 概述

本文档定义从 RDP Phase 6 产出的 recommendation 到实际影响生产系统参数的完整受控流程。

**核心原则: 建议生成与参数应用分离。**

```
Phase 2/3/4/5/6
  → generate recommendation (draft)
  → reviewer/operator approve
  → apply active parameter set
  → production restart / reload
  → observe
```

Phase 6 **不直接修改生产参数**，必须经过人工审批。

## 2. 状态定义

### 2.1 Recommendation 状态

| 状态 | 说明 |
|------|------|
| `draft` | 系统生成，待审批 |
| `approved` | 已审批，可应用 |
| `rejected` | 已拒绝 |
| `superseded` | 被后续 recommendation 取代 |

### 2.2 Active Parameter 状态

| 状态 | 说明 |
|------|------|
| `active` | 当前生效中 |
| (不存在) | 无 active set，使用默认配置 |

### 2.3 Parameter Set 状态

| 状态 | 说明 |
|------|------|
| `draft` | 初始导入，未确认 |
| `candidate` | 候选，待验证 |
| `frozen` | 已冻结，可用于生产 |
| `deprecated` | 已弃用 |

## 3. 完整工作流

### 步骤 1: 运行 Decision Round

```bash
python scripts/rdp_run_decision_round.py
```

产出:
- evidence_bundle_summary.json
- parameter_upgrade_candidates.json
- family_timeframe_decisions.json
- promotion_readiness_assessment.json
- phase6_closed_loop_decision_conclusion.md

同时更新:
- recommendation_registry.json (新增 draft recommendations)
- active_decision_registry.json (更新 family/tf 状态)

### 步骤 2: 审查 Recommendations

```bash
python scripts/approve_recommendation_and_apply.py --action list
```

输出所有 draft/approved/rejected recommendations，按状态分组。

### 步骤 3: 审批或拒绝

**审批:**
```bash
# 仅审批（不立即应用参数）
python scripts/approve_recommendation_and_apply.py \
    --action approve \
    --rec-id rec_20260404_153614_abc123 \
    --reason "reviewed decision round report, evidence sufficient"

# 审批并立即应用关联参数
python scripts/approve_recommendation_and_apply.py \
    --action approve-and-apply \
    --rec-id rec_20260404_153614_abc123 \
    --reason "approved for production deployment"
```

**拒绝:**
```bash
python scripts/approve_recommendation_and_apply.py \
    --action reject \
    --rec-id rec_20260404_153614_abc123 \
    --reason "insufficient evidence for parameter change"
```

### 步骤 4: 应用参数（如果步骤 3 未同时应用）

```bash
# 查看可用的 frozen parameter sets
python scripts/apply_active_parameter_set.py --action show

# 应用指定参数
python scripts/apply_active_parameter_set.py \
    --action apply \
    --ps-id ps_20260404_072612_a5cc10 \
    --recommendation-id rec_20260404_153614_abc123
```

### 步骤 5: 重启 / Reload

如果主交易系统正在运行，需要重启或 reload 使新参数生效:

```bash
# 方式 1: 重启 API gateway
python scripts/start_api.py --profile derivatives

# 方式 2: 调用 rebaseline API
curl -X POST http://localhost:8000/system/rebaseline \
    -H "Content-Type: application/json" \
    -d '{"reason": "apply new active parameter set"}'
```

### 步骤 6: 观察

通过 Operator API 观察新参数的效果:

```bash
# 查看当前 active 参数
curl http://localhost:8000/rdp/parameters/active

# 查看最新决策
curl http://localhost:8000/rdp/decisions/latest

# 查看健康状态
curl http://localhost:8000/rdp/health
```

## 4. 安全检查清单

在审批和应用参数前，operator 应确认:

- [ ] Decision Round 报告已阅读
- [ ] Evidence Bundle 数据覆盖充分
- [ ] Quality Monitor 状态为 healthy
- [ ] 参数变更幅度在合理范围内
- [ ] 无 severe negative signal
- [ ] 目标 parameter set 为 frozen 或 candidate
- [ ] 关联的 family/timeframe 决策不是 pause

## 5. 回滚流程

如果新参数导致问题:

```bash
# 1. 清除问题 active set
python scripts/apply_active_parameter_set.py \
    --action clear \
    --combo independent_15m

# 2. 或者应用上一个已知安全的 parameter set
python scripts/apply_active_parameter_set.py \
    --action apply \
    --ps-id ps_previous_known_good

# 3. 重启主系统
python scripts/start_api.py --profile derivatives
```

## 6. 审计追踪

所有操作都有审计日志:

| 日志文件 | 内容 |
|---------|------|
| `artifacts/governance/application_logs/parameter_application_history.jsonl` | 参数应用历史 |
| `artifacts/governance/approval_logs/recommendation_approval_history.jsonl` | 审批操作历史 |
| `artifacts/decision_rounds/<round_id>/` | 每次 decision round 的完整产物 |

## 7. 不允许的操作

1. **禁止** Phase 6 自动修改生产参数
2. **禁止** 跳过审批直接应用 draft recommendation
3. **禁止** 直接修改 `configs/active_parameter_sets/` 目录下的文件（应通过脚本操作）
4. **禁止** 将 deprecated parameter set 应用为 active
5. **禁止** 在 quality monitor 状态为 unhealthy 时审批参数

## 8. 自动化路线图

第一版要求完全人工审批。未来可考虑:

1. **半自动审批**: 如果 promotion readiness = ready_for_next_live_test 且 confidence = high，自动进入审批队列
2. **自动 pause**: 如果 decision_engine 连续 N 轮建议 pause，自动暂停
3. **自动通知**: 新 recommendation 产生时发送通知

但以上功能需要额外的安全机制，不在第一版范围内。
