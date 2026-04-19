# Parameter Apply & Rollback 操作指南

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 本文档描述如何将已批准的 recommendation 受控地应用为 active parameter set，
> 以及如何在出问题时回滚。

---

## 1. 设计原则

- **apply 必须是显式动作** — recommendation 不会自动生效
- **apply 必须可审计** — 每次操作记录在 `parameter_apply_history.json`
- **apply 必须可回滚** — 任何 apply 都可以被回滚到上一版本
- **生产 apply 必须经过 gate** — live 不提供跳过 gate 的标准流程
- **生产 direct apply 默认冻结** — `RDP_ENV=prod` 下应通过 release 流程触发，且需显式设置 `RDP_PRODUCTION_APPLY_ENABLED=true`

---

## 2. Apply 流程

### 2.1 前置条件

1. recommendation 状态必须为 `approved`
2. recommendation 必须有 `target_parameter_set_id`
3. target parameter set 必须在 `artifacts/governance/current_parameter_registry.json` 中存在

### 2.2 Apply 行为

```
approved recommendation
    ↓
解析 target_parameter_set_id
    ↓
从 parameter_registry 获取 values（DB 优先 → 文件 fallback）
    ↓
写入 governance.active_parameter_sets (DB)          ← DB 双写
    ↓
写入 configs/active_parameter_sets/active_parameter_registry.json (文件备份)
    ↓
写入 configs/active_parameter_sets/<combo>.json (per-file 备份)
    ↓
写入 governance.parameter_apply_history (DB)        ← DB 双写
    ↓
写入 artifacts/decision_system/parameter_apply_history.json (文件备份)
    ↓
输出后续重启/reload 指令
```

> **DB 开关**: 当 `AATS_ACTIVE_PARAMETER_DB_URL` 环境变量未设置时，跳过 DB 写入，仅走文件路径。

### 2.3 通过脚本 Apply

> 仅适用于 `dev`，或已准备好完整 gate/release 上下文的非生产调试场景。`prod` 下 direct apply 会被拒绝，请改走 `scripts/rdp_create_parameter_release.py`。

```bash
python scripts/rdp_apply_approved_recommendation.py \
    --recommendation-id rec_xxx \
    --actor operator_wang \
    --notes "SOP 审批通过后应用"
```

### 2.4 通过 API Apply

> 仅适用于 `dev`，或内部受控调用。`prod` 下 `/rdp/parameters/apply` 会被拒绝。

```bash
curl -X POST /rdp/parameters/apply \
    -H "Content-Type: application/json" \
    -d '{
        "recommendation_id": "rec_xxx",
        "actor": "operator_wang",
        "notes": "SOP approved"
    }'
```

### 2.5 Apply 后操作

apply 只修改配置文件，不会自动重启系统。需要：

- **方式 1**: 重启 API gateway（下次 `build_runtime()` 会加载新参数）
- **方式 2**: 调用 `POST /system/rebaseline`（如果已实现热加载）

---

## 3. Rollback 流程

### 3.1 Rollback 行为

```
当前 active parameter set
    ↓
从 apply history 查找上一个版本（或指定版本）
    ↓
从 parameter_registry 获取该版本的 values
    ↓
重新写为 active
    ↓
写入 rollback history
    ↓
输出结果
```

### 3.2 通过脚本 Rollback

```bash
# 自动回滚到上一版本
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m \
    --actor operator_wang

# 回滚到指定版本
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m \
    --to-parameter-set-id ps_20260403_xxx \
    --actor operator_wang

# 预览
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m --dry-run
```

### 3.3 通过 API Rollback

```bash
curl -X POST /rdp/parameters/rollback \
    -H "Content-Type: application/json" \
    -d '{
        "family": "independent",
        "timeframe": "15m",
        "actor": "operator_wang",
        "notes": "live behavior degraded"
    }'
```

---

## 4. Apply History

所有 apply / rollback / clear 操作记录在：

```
artifacts/decision_system/parameter_apply_history.json
```

### 记录格式

```json
{
  "operation_id": "op_20260404_163000_abc123",
  "operation_type": "apply",
  "family": "independent",
  "timeframe": "15m",
  "from_parameter_set_id": "ps_old_xxx",
  "to_parameter_set_id": "ps_new_yyy",
  "recommendation_id": "rec_xxx",
  "actor": "operator_wang",
  "created_at": "2026-04-04T16:30:00+00:00",
  "notes": "approved and applied"
}
```

### 查看历史

```bash
# API
curl GET /rdp/parameters/apply-history

# 脚本
python scripts/apply_active_parameter_set.py --action show-active

# 全量种子到 DB（从 JSON 文件同步到 DB，幂等可重复）
python scripts/apply_active_parameter_set.py --action seed-db
```

---

## 5. Active Registry 更新逻辑

apply 和 rollback 同时更新三层存储：

1. **DB 表**（主）: `governance.active_parameter_sets`（设置 `AATS_ACTIVE_PARAMETER_DB_URL` 后启用）
2. **Registry 格式**（文件备份）: `configs/active_parameter_sets/active_parameter_registry.json`
3. **Per-file 格式**（兼容备份）: `configs/active_parameter_sets/<family>_<timeframe>.json`

三层始终保持同步。主系统加载时优先读 DB → registry 文件 → per-file fallback。

> **历史记录同理**: `governance.parameter_apply_history` DB 表 + `parameter_apply_history.json` 文件始终双写。

---

## 6. 回滚触发条件

以下情况建议立即回滚：

| 条件 | 严重度 | 操作 |
|------|--------|------|
| live 行为明显恶化（PnL、成交率） | 高 | 立即回滚 |
| attribution 出现新的主要失败类型 | 中 | 评估后回滚 |
| execution realism 与预期偏差 > 2x | 中 | 评估后回滚 |
| operator / reviewer 明确要求 | 高 | 立即回滚 |
| quality monitor 报告 unhealthy | 中 | 评估后回滚 |

---

## 7. 注意事项

1. **不要跳过 approval 直接 apply** — 必须先 approve recommendation
2. **不要在交易活跃期 apply** — 建议在低波动时段操作
3. **apply 后观察至少 1 个交易周期** — 确认参数效果
4. **回滚后也要重启** — rollback 同样需要重启/reload 使参数生效
5. **保留 history** — 不要手动删除 `parameter_apply_history.json`
6. **生产不要跳过 gate** — gate 记录缺失时，不应继续 apply

---

## 8. 交易系统安全关联

active parameter set 会在主交易系统 `build_runtime()` 期间注入策略参数。参数 apply 成功不等于 live 安全，apply 后必须检查：

- active parameter registry 中 family/timeframe 指向的新版本。
- apply history 中 actor、recommendation、gate status、notes。
- 主交易系统 `/system/health`。
- 最近 decision frequency、order intent 数量、reconciliation 状态。
- 如果 live 行为异常，先执行 rollback，再调查 RDP evidence。

相关流程见 [`production_parameter_change_runbook.md`](production_parameter_change_runbook.md)。
