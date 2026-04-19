# RDP Operator 工作流 SOP

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 本文档让 operator 不依赖口头知识即可执行完整的 RDP 运营流程：
> 看结论、批 recommendation、apply 参数、rollback、日常巡检。

---

## 1. 背景与目标

RDP（Research Data Platform）持续研究并产出治理建议，
但这些建议不会自动生效。需要 operator 按照受控流程：

1. **看到** — 查看 RDP 最新结论
2. **批准** — 审批/拒绝 recommendation
3. **应用** — 将已批准参数写入 active parameter set
4. **观察** — 确认 apply 后的效果
5. **回滚** — 出问题时回退参数

---

## 2. Daily / Weekly Operator 检查项

### 2.1 每日检查

| 检查项 | 操作 | 关注点 |
|--------|------|--------|
| RDP 健康状态 | `GET /rdp/health` | overall_health 是否 healthy |
| 待审批建议 | `GET /rdp/recommendations/latest?status=draft` | 是否有新的 draft recommendation |
| Active 参数状态 | `GET /rdp/parameters/active` | missing_combos 是否增加 |
| 最近操作历史 | `GET /rdp/parameters/apply-history` | 是否有异常操作 |

### 2.2 每周检查

| 检查项 | 操作 | 关注点 |
|--------|------|--------|
| Attribution 趋势 | `GET /rdp/attribution/latest` | failure mode 结构是否变化 |
| Execution Realism | `GET /rdp/execution/latest` | 执行成本是否异常升高 |
| Family/TF 决策 | `GET /rdp/decisions/latest` | 是否有 combo 被标记为 pause/require_review |
| Readiness 评估 | `GET /rdp/readiness` | 是否有新的升级就绪 combo |
| 完整决策轮次 | `GET /rdp/decision-round/latest` | 最近 round 是否正常完成 |

---

## 3. Recommendation Review Checklist

当收到新的 draft recommendation 时：

### 3.1 信息收集

- [ ] 确认 recommendation_type（parameter_upgrade / keep_active / pause / etc.）
- [ ] 确认 target family/timeframe
- [ ] 确认 confidence level（low / medium / high）
- [ ] 查看 reason 字段了解建议依据
- [ ] 如果是 parameter_upgrade，确认 target_parameter_set_id 存在

### 3.2 交叉验证

- [ ] 查看 `GET /rdp/attribution/latest`：归因分析是否支持该建议
- [ ] 查看 `GET /rdp/execution/latest`：执行真实性是否正常
- [ ] 查看 `GET /rdp/decisions/latest`：该 combo 当前状态是否一致
- [ ] 查看 `GET /rdp/health`：RDP 系统是否健康

---

## 4. Approval Checklist

决定是否审批时，按以下清单逐项确认：

### 4.1 必须满足（全部 Yes 才能 approve）

- [ ] recommendation confidence >= medium
- [ ] evidence completeness 足够（evidence_bundle 有多个 phase 数据）
- [ ] quality monitor 报告 healthy（无数据异常）
- [ ] attribution 和 execution realism 无明显冲突
- [ ] 同一 combo 没有已批准但未 apply 的 recommendation

### 4.2 建议满足

- [ ] target parameter set 状态为 frozen（而非仅 candidate）
- [ ] 至少有 2 轮 decision round 给出一致建议
- [ ] 最近 24h 内没有重大市场事件

### 4.3 执行审批

```bash
# 审批通过
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_xxx \
    --action approve \
    --actor <your_name> \
    --notes "checklist passed, confidence medium, evidence complete"

# 审批拒绝
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_xxx \
    --action reject \
    --actor <your_name> \
    --notes "evidence incomplete, confidence low"
```

或通过 API：

```bash
curl -X POST /rdp/recommendations/rec_xxx/approve \
    -d '{"actor": "<your_name>", "notes": "checklist passed"}'
```

---

## 5. Apply Checklist

recommendation 审批通过后，按以下清单应用参数：

### 5.1 Apply 前确认

- [ ] recommendation 状态确实为 approved
- [ ] 当前不在交易活跃期（建议在 UTC 00:00-04:00 低波动时段）
- [ ] 已通知相关团队成员
- [ ] 有 rollback 计划（知道上一版参数是什么）

### 5.2 执行 Apply

```bash
# 先预览
python scripts/rdp_apply_approved_recommendation.py \
    --recommendation-id rec_xxx --dry-run

# 确认无误后执行
python scripts/rdp_apply_approved_recommendation.py \
    --recommendation-id rec_xxx \
    --actor <your_name> \
    --notes "applying per SOP"
```

### 5.3 Apply 后确认

- [ ] `GET /rdp/parameters/active` 显示新参数已生效
- [ ] `GET /rdp/parameters/apply-history` 有新的 apply 记录
- [ ] 重启主交易系统（或调用 rebaseline）
- [ ] 确认系统正常启动

---

## 6. Apply 后观察

apply 参数后，operator 应持续观察至少 1 个完整交易周期：

### 6.1 观察项

| 时间点 | 检查项 | 预期 |
|--------|--------|------|
| Apply 后 1h | 系统是否正常运行 | 无错误日志 |
| Apply 后 4h | PnL 趋势 | 无明显恶化 |
| Apply 后 24h | Attribution failure modes | 与 apply 前对比，目标 failure mode 改善 |
| Apply 后 48h | Execution realism | 执行成本无异常升高 |
| Apply 后 1 week | Readiness 评估 | 整体 readiness 不下降 |

### 6.2 关注信号

以下信号表明可能需要回滚：

- PnL 连续 4h 低于 apply 前均值
- 新增之前未见的 attribution failure mode
- execution cost 升高 > 50%
- quality monitor 报告 degraded 或 unhealthy

---

## 7. Rollback Checklist

需要回滚时，按以下清单操作：

### 7.1 何时 Rollback

| 触发条件 | 紧急度 | 行动 |
|----------|--------|------|
| live 行为明显恶化（PnL 连续下降） | **紧急** | 立即回滚 |
| attribution 出现新的主要失败类型 | 中 | 评估后回滚 |
| execution realism 与预期偏差 > 2x | 中 | 评估后回滚 |
| operator / reviewer 明确要求 | **紧急** | 立即回滚 |
| quality monitor 报告 unhealthy | 中 | 评估后回滚 |

### 7.2 执行 Rollback

```bash
# 先预览
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m --dry-run

# 确认后执行
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m \
    --actor <your_name> \
    --notes "PnL degraded after apply, rolling back"
```

### 7.3 Rollback 后操作

- [ ] 重启主交易系统
- [ ] 确认 `GET /rdp/parameters/active` 显示回滚后的参数
- [ ] 确认 `GET /rdp/parameters/apply-history` 有 rollback 记录
- [ ] 通知团队已回滚
- [ ] 在 recommendation 上添加备注说明回滚原因

---

## 8. 常见异常处理

### 8.1 recommendation 找不到

```
[ERROR] 未找到 recommendation: rec_xxx
```

**原因**: recommendation_id 拼写错误，或该 recommendation 不在 registry 中。

**处理**: 用 `GET /rdp/recommendations/latest?limit=100` 查看所有 recommendation。

### 8.2 recommendation 状态非 draft

```
[WARNING] recommendation 状态为 'approved'（非 draft）
```

**原因**: 该 recommendation 已被审批过。

**处理**: 如需重新审批，应创建新的 recommendation（不要修改已审批的）。

### 8.3 parameter set 找不到

```
[ERROR] parameter_registry 中未找到 ps_xxx
```

**原因**: target parameter set 可能已被 deprecated 或从未在 registry 中注册。

**处理**: 用 `python scripts/apply_active_parameter_set.py --action show` 查看所有可用参数。

### 8.4 没有可回滚的历史版本

```
[ERROR] independent_15m 没有可回滚的历史版本
```

**原因**: 该 combo 只 apply 过一次，没有更早的版本。

**处理**: 使用 `--to-parameter-set-id` 指定一个具体的 parameter set id 进行回滚，
或使用 `--action clear --combo independent_15m` 清除 active set（回退到 profile 默认值）。

### 8.5 apply 后系统未生效

**原因**: 主交易系统在启动时加载参数，运行期间不会自动检测文件变更。

**处理**: 重启 API gateway 或调用 `POST /system/rebaseline`。

---

## 9. 与 Artifacts / Registry 的对应关系

> 自 2026-04-11 起，全部注册表采用 **DB-first + 文件 fallback** 双写模式。
> 设置 `AATS_ACTIVE_PARAMETER_DB_URL` 后，API/脚本优先读 DB。

| JSON 文件（文件备份） | DB 表（主存储） | 用途 | 谁写 | 谁读 |
|------|------|------|------|------|
| `artifacts/governance/current_parameter_registry.json` | `governance.parameter_sets` | 所有参数集版本 | research pipeline | apply 脚本 |
| `artifacts/decision_system/recommendation_registry.json` | `governance.recommendations` | 所有 recommendation | decision round / 审批脚本 | operator API |
| `artifacts/decision_system/active_decision_registry.json` | `governance.active_decisions` | family/tf 运营状态 | decision round | operator API |
| `configs/active_parameter_sets/active_parameter_registry.json` | `governance.active_parameter_sets` | 当前 active 参数 | apply/rollback | 主交易系统 |
| `artifacts/decision_system/parameter_apply_history.json` | `governance.parameter_apply_history` | apply/rollback 操作记录 | apply/rollback 脚本 | operator API |
| `artifacts/governance/approval_logs/*.jsonl` | （无 DB 表） | 审批审计日志 | 审批脚本 | 审计 |

### 全量种子

首次启用 DB 或 DB 数据丢失后，可一键从 JSON 文件同步到 DB（幂等）:

```bash
python scripts/apply_active_parameter_set.py --action seed-db
```

---

## 10. 快速参考

```bash
# ── 查看 ──
GET /rdp/health                            # 系统健康
GET /rdp/parameters/active                 # 当前参数
GET /rdp/recommendations/latest?status=draft  # 待审批建议
GET /rdp/parameters/apply-history          # 操作历史

# ── 审批 ──
POST /rdp/recommendations/{id}/approve     # 审批
POST /rdp/recommendations/{id}/reject      # 拒绝
POST /rdp/recommendations/{id}/supersede   # 替代

# ── 应用 ──
POST /rdp/parameters/apply                 # 应用已批准参数
POST /rdp/parameters/rollback              # 回滚

# ── 脚本 ──
python scripts/rdp_approve_recommendation.py --recommendation-id X --action approve --actor Y
python scripts/rdp_apply_approved_recommendation.py --recommendation-id X --actor Y
python scripts/rdp_rollback_active_parameter_set.py --family F --timeframe T --actor Y
```
