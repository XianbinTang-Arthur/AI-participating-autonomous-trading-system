# RDP 硬化 · 批次 A 详细设计

> **文档状态**：待审批（2026-04-17 起草）
> **上级文档**：[rdp_full_hardening_sow.md](rdp_full_hardening_sow.md)
> **工期估计**：3-5 天（含 DB 迁移窗口与集成测试）
> **目标**：切断所有可绕过治理改 live 参数的路径 + 在 DB 层强制业务不变量

---

## 1. 批次 A 概览

### 1.1 子任务清单

| 编号 | 标题 | 预估工时 | 依赖 |
|------|------|---------|------|
| A-0.1 | Rollback 目标校验收口 | 0.5 天 | A-1（需 DB FK 支持）|
| A-0.2 | Legacy 脚本全面禁用 | 0.5 天 | 独立 |
| A-0.3 | 清扫 "DB→JSON 成功" 反模式 | 1 天 | 独立 |
| A-0.4 | Gate ISO 时间解析统一 | 0.25 天 | 独立 |
| A-0.5 | `RDP_PRODUCTION_APPLY_ENABLED` → short-lived token | 0.75 天 | 独立 |
| A-0.6 | `apply-frozen` 动作物理删除 | 合入 A-0.2 | 无 |
| A-1 | DB schema 硬化（7 条 DDL + 数据清理）| 1.5 天 | 必须在 A-0.1 之前 |

**执行顺序建议**：A-1（DB 硬化）→ A-0.3（降级清扫）→ A-0.1（rollback 收口）→ A-0.4（时区统一）→ A-0.5（token）→ A-0.2（脚本禁用）

理由：A-1 完成后 A-0.1 的 DB 校验才能依赖 FK；A-0.2 最后做避免中途因脚本禁用无法做数据清理。

### 1.2 涉及的主要文件清单（全局一览）

| 类别 | 文件 | 变更类型 |
|------|------|---------|
| DB ORM | `aats/data_platform/rdp_models.py` | 补 FK/CHECK/UQ |
| DB 迁移 runner | `aats/data_platform/db.py` | 新增 `apply_batch_a_migrations()` |
| 新增迁移 SQL | `aats/data_platform/migrations/batch_a_*.sql` | 新建 |
| Rollback 逻辑 | `aats/data_platform/decision_system/active_parameter_apply.py` | 改写 `rollback_active_parameter_set` |
| Rollback 历史查询 | `aats/data_platform/governance/active_params_db.py` | 改写 `db_get_previous_set_id` |
| Recommendation 状态机 | `aats/data_platform/decision_system/recommendation_registry.py` | 改写 `_db_update_rec_status` 返回语义 + 调用方 |
| Release 状态机 | `aats/data_platform/decision_system/release_registry.py` | 同款改写 |
| Active Decision | `aats/data_platform/decision_system/active_decision_registry.py` | 同款改写 |
| Evidence Bundle | `aats/data_platform/decision_system/evidence_bundle_index.py` | 同款改写 |
| 时间工具 | `aats/data_platform/governance/_time_util.py` | 新建 |
| Gate 规则 | `aats/data_platform/production_workflow/gate_rules.py` | 改用新工具 |
| Gate Runtime Contract | `aats/data_platform/production_workflow/gate_runtime_contract.py` | 改用新工具 |
| Token 签发 | `aats/api/rdp_apply_token.py` | 新建 |
| API 路由 | `aats/api/rdp_routes.py` | 接入 token 校验；环境 flag 清理 |
| 操作员 CLI token | `scripts/rdp_emit_apply_token.py` | 新建 |
| Legacy 脚本 stub | `scripts/apply_active_parameter_set.py` 等 9 个 | 改为 exit 2 |
| 集成测试 | `tests/integration/test_rdp_batch_a_*.py` | 新建 5 份 |
| 单元测试 | `tests/unit/test_rdp_batch_a_*.py` | 新建 6 份 |

---

## 2. A-0.1 Rollback 目标校验收口

### 2.1 背景

用户审查指出（P0-1）：`rollback_active_parameter_set` 在提供 `to_parameter_set_id` 时仅校验其在 JSON registry 中存在，不校验它属于该 family 的"已批准历史 lineage"。Claude 深挖进一步发现：目标参数集是**从本地 JSON `current_parameter_registry.json` 读出**的，如果该 JSON 被污染（比如阶段 1 前的双写路径遗留），rollback 直接写污染值到 DB，形成**注入通道**。

### 2.2 现状代码（证据）

[active_parameter_apply.py:447-471](../../aats/data_platform/decision_system/active_parameter_apply.py)：

```python
if to_parameter_set_id is None:
    with get_session() as session:
        to_parameter_set_id = db_get_previous_set_id(session, family, timeframe)

gov_reg_path = project_root / GOVERNANCE_DIR / "current_parameter_registry.json"
gov_registry = load_registry(gov_reg_path)
target_ps = None
for ps in gov_registry.get("parameter_sets", []):
    if ps["parameter_set_id"] == to_parameter_set_id:
        target_ps = ps
        break
if target_ps is None:
    return {"ok": False, "message": f"parameter_registry 中未找到回滚目标 {to_parameter_set_id}"}
```

[active_params_db.py:239-254](../../aats/data_platform/governance/active_params_db.py) 的 `db_get_previous_set_id`：

```sql
SELECT from_parameter_set_id FROM governance.parameter_apply_history
WHERE family = :family AND timeframe = :timeframe
  AND operation_type IN ('apply', 'rollback')
ORDER BY created_at DESC LIMIT 1
```
无 `SELECT FOR UPDATE` —— 存在竞态窗口。

### 2.3 目标状态

Rollback 接受的 `to_parameter_set_id` 必须**同时**满足：

1. **存在于 DB**：`governance.parameter_sets` 表中有该 id
2. **状态合法**：`status IN ('frozen', 'released')`，不能是 `draft`、`candidate`、`deprecated`
3. **归属正确**：`family` 和 `timeframe` 与当前回滚请求匹配
4. **历史凭证**：在 `parameter_apply_history` 中，该 id 至少作为该 family+timeframe 的一次 `apply` 操作的 `to_parameter_set_id` 出现过（证明它"曾是 live"）
5. **不是当前生效**：`active_parameter_sets` 当前 `parameter_set_id` ≠ 目标 id（避免自回滚）
6. **批准链路**：通过 `target_parameter_set_id` 反查 `recommendations` 表，至少存在一条 `status IN ('approved','applied','rolled_back')` 的 recommendation 指向该 parameter_set_id

不满足任一条：返回 **422 Unprocessable Entity**，写入 `rollback_recommendations` 表 `severity='rejected'` + 原因。

不提供 `to_parameter_set_id` 时，从 `parameter_apply_history` 推导前值的查询必须加 `FOR UPDATE`（批次 A-1 之后依赖 FK 确保推导结果合法）。

### 2.4 变更清单

#### 2.4.1 新增函数 `validate_rollback_target`

**文件**：`aats/data_platform/governance/active_params_db.py`
**位置**：在 `db_get_previous_set_id` 之前插入

```python
def validate_rollback_target(
    session,
    family: str,
    timeframe: str,
    target_parameter_set_id: str,
) -> tuple[bool, str]:
    """校验 rollback 目标合法性。返回 (ok, reason_if_rejected)。

    见批次 A 详设 §2.3 的 6 条校验规则。
    """
    # ... 6 条 SELECT 校验 ...
```

校验逻辑伪码：

```python
# 规则 1+2+3: parameter_sets 存在 + 状态合法 + 归属正确
row = session.execute(text("""
    SELECT status FROM governance.parameter_sets
    WHERE parameter_set_id = :pid AND family = :family AND timeframe = :tf
"""), {"pid": target_parameter_set_id, "family": family, "tf": timeframe}).first()
if row is None:
    return False, "target_not_found_or_wrong_combo"
if row.status not in ("frozen", "released"):
    return False, f"target_status_illegal:{row.status}"

# 规则 4: 历史凭证
history_row = session.execute(text("""
    SELECT 1 FROM governance.parameter_apply_history
    WHERE family = :family AND timeframe = :tf
      AND operation_type = 'apply'
      AND to_parameter_set_id = :pid
    LIMIT 1
"""), {...}).first()
if history_row is None:
    return False, "no_apply_history_for_target"

# 规则 5: 不是当前生效
current_row = session.execute(text("""
    SELECT parameter_set_id FROM governance.active_parameter_sets
    WHERE family = :family AND timeframe = :tf
    FOR UPDATE
"""), {...}).first()
if current_row is not None and current_row.parameter_set_id == target_parameter_set_id:
    return False, "target_is_currently_active"

# 规则 6: 批准链路
rec_row = session.execute(text("""
    SELECT 1 FROM governance.recommendations
    WHERE target_parameter_set_id = :pid
      AND family = :family AND timeframe = :tf
      AND status IN ('approved','applied','rolled_back')
    LIMIT 1
"""), {...}).first()
if rec_row is None:
    return False, "no_approved_recommendation_lineage"

return True, ""
```

#### 2.4.2 改写 `db_get_previous_set_id` 加排他锁

**文件**：同上
**变更**：

```python
# 原
SELECT from_parameter_set_id FROM governance.parameter_apply_history
WHERE family = :family AND timeframe = :timeframe
  AND operation_type IN ('apply', 'rollback')
ORDER BY created_at DESC LIMIT 1

# 改为
SELECT from_parameter_set_id FROM governance.parameter_apply_history
WHERE family = :family AND timeframe = :timeframe
  AND operation_type IN ('apply', 'rollback')
ORDER BY created_at DESC LIMIT 1
FOR UPDATE
```

#### 2.4.3 改写 `rollback_active_parameter_set`

**文件**：`aats/data_platform/decision_system/active_parameter_apply.py`
**行范围**：约 420-500（完整函数）

**核心变更**：

1. **目标不再从 JSON registry 读**：直接从 `parameter_sets` 表 SELECT values
2. **前置校验调用 `validate_rollback_target`**
3. **整个流程在单一事务内**（`with get_session() as session`），确保校验到写入之间无并发窗口

改写后伪码：

```python
def rollback_active_parameter_set(
    family: str,
    timeframe: str,
    to_parameter_set_id: Optional[str] = None,
    actor: str = "operator",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    with get_session() as session:
        # 1) 推导目标（如未提供）
        if to_parameter_set_id is None:
            to_parameter_set_id = db_get_previous_set_id(session, family, timeframe)
            if to_parameter_set_id is None:
                return {"ok": False, "code": "NO_PREVIOUS_TARGET"}

        # 2) 强校验
        ok, reason = validate_rollback_target(session, family, timeframe, to_parameter_set_id)
        if not ok:
            # 写入 rollback_recommendations 表作为拒绝审计
            _record_rollback_rejection(session, family, timeframe, to_parameter_set_id, reason, actor)
            return {"ok": False, "code": "VALIDATION_FAILED", "reason": reason}

        # 3) 从 DB 读目标 values（不再读 JSON）
        target_row = session.execute(text("""
            SELECT values, source_round_id, approval_recommendation_id
            FROM governance.parameter_sets
            WHERE parameter_set_id = :pid
        """), {"pid": to_parameter_set_id}).first()

        # 4) 写入 active_parameter_sets + history（原子事务）
        db_upsert_active_set(session, family, timeframe, target_row.values, ...)
        db_append_history(session, ..., operation_type='rollback', ...)

        # 5) 文件审计副本（best-effort，失败仅 warn，不回滚）
        try:
            _write_file_audit(...)
        except Exception as exc:
            log.warning("rollback file audit failed: %s", exc)

        return {"ok": True, "to_parameter_set_id": to_parameter_set_id, ...}
```

#### 2.4.4 API 层错误码映射

**文件**：`aats/api/rdp_routes.py`，rollback 路由处
**变更**：

- `NO_PREVIOUS_TARGET` → 422 + `{"error": "no_previous_target"}`
- `VALIDATION_FAILED` → 422 + `{"error": "validation_failed", "reason": reason}`
- 任何 DB 错误 → 500（不再 fallback 到文件）

### 2.5 测试

**单元测试**（`tests/unit/test_rdp_rollback_validation.py`）：

```python
def test_rollback_rejects_nonexistent_target()
def test_rollback_rejects_target_from_different_family()
def test_rollback_rejects_target_from_different_timeframe()
def test_rollback_rejects_deprecated_target()
def test_rollback_rejects_draft_target()
def test_rollback_rejects_target_without_apply_history()
def test_rollback_rejects_target_without_approved_recommendation()
def test_rollback_rejects_self_rollback()
def test_rollback_accepts_valid_frozen_target_with_lineage()
```

**集成测试**（`tests/integration/test_rdp_rollback_with_real_db.py`，testcontainers）：

```python
def test_rollback_scenario_end_to_end():
    # 1. 造 3 个 parameter_sets (v1, v2, v3)
    # 2. 造 apply history: v0 → v1 → v2 → v3
    # 3. 造 3 条 approved recommendations 指向 v1/v2/v3
    # 4. rollback to v2 应成功
    # 5. rollback to v99 (不存在) 应 422
    # 6. rollback to draft_v4 应 422
    # 7. rollback to cross_family v2_x 应 422
```

### 2.6 回滚

单独 revert 本项对应的 commit 即可；不依赖其他批次 A 子项（除 A-1 DB FK 需要先存在）。

---

## 3. A-0.2 Legacy 脚本全面禁用

### 3.1 背景

用户决策（#2）：**直接禁用脚本只留 API（最严）**。

所有写类 `rdp_*.py` 脚本 + `apply_active_parameter_set.py` + `approve_recommendation_and_apply.py` 改为 exit 2 + 提示信息。

### 3.2 脚本分类（开工前确认）

**必须禁用（写治理状态）**：9 个

| 脚本 | 原功能 | 替代 API |
|------|--------|---------|
| `scripts/apply_active_parameter_set.py` | 直写 active + apply-frozen | `POST /rdp/parameters/apply` |
| `scripts/approve_recommendation_and_apply.py` | 组合审批+apply | `POST /rdp/recommendations/{id}/approve` + `POST /rdp/releases` |
| `scripts/rdp_apply_approved_recommendation.py` | 应用已批准 recommendation | `POST /rdp/releases` |
| `scripts/rdp_approve_recommendation.py` | 审批 recommendation | `POST /rdp/recommendations/{id}/approve` |
| `scripts/rdp_rollback_active_parameter_set.py` | 回滚参数集 | `POST /rdp/parameters/rollback` |
| `scripts/rdp_freeze_parameter_set.py` | 冻结 parameter set | `POST /rdp/parameters/freeze` |
| `scripts/rdp_create_parameter_release.py` | 创建 release | `POST /rdp/releases` |
| `scripts/rdp_run_release_cycle.py` | 跑 release cycle workflow | 通过 daemon task queue |
| `scripts/rdp_update_decision_registry.py` | 更新决策注册表 | `POST /rdp/decisions/update` |

**保留（只读或数据摄取）**：其余 51 个 `rdp_*.py` 脚本不受影响，包括：
- `rdp_task_daemon.py`（daemon 进程入口）
- `rdp_run_scheduled_workflow.py`（daemon 内部调用）
- 所有 `rdp_run_*` 研究流程（Phase 2/3/4 round runner）
- 所有 `rdp_backfill_*`、`rdp_build_*`、`rdp_detect_*` 摄取/只读脚本

### 3.3 Stub 模板

**每个禁用脚本改为**：

```python
#!/usr/bin/env python3
"""[DEPRECATED since batch A — 2026-04-??]

原功能：<一句话描述>
替代路径：<API 或 workflow 说明>

直接调用将 exit 2。如因 crontab/CI 依赖需过渡，请联系维护者。
"""
from __future__ import annotations
import sys

DEPRECATION_MESSAGE = (
    "此脚本已在 RDP 批次 A 硬化中禁用。\n"
    "原功能：<...>\n"
    "请改用：<API 端点>\n"
    "如需紧急操作员通道，请使用 `scripts/rdp_emit_apply_token.py` 获取 token "
    "并通过 API 调用。\n"
    "相关文档：docs/task/rdp_hardening_batch_a_detailed_design.md §3"
)

if __name__ == "__main__":
    print(DEPRECATION_MESSAGE, file=sys.stderr)
    sys.exit(2)
```

### 3.4 `apply-frozen` 动作物理删除（A-0.6 合入此处）

由于 `apply_active_parameter_set.py` 整体 stub 化，其中的 `action_apply_frozen` 函数（原 line 295-357）连同 "bypassed_frozen" 常量一并删除。

**额外清理**（grep 确认无遗留）：
- 搜 `bypassed_frozen` 在 `aats/` 下的使用点，逐个替换或删除
- 搜 `apply-frozen` 字符串（argparse 参数名）
- 搜 `action_apply_frozen` 函数引用

### 3.5 变更清单

1. 9 个脚本改为 stub（按 §3.3 模板）
2. `grep -rn "bypassed_frozen\|apply-frozen" aats/ scripts/` 的所有命中点清理
3. CI 加 grep 守门（见 §9.2）

### 3.6 测试

**冒烟测试**（`tests/smoke/test_rdp_legacy_scripts_disabled.sh`）：

```bash
#!/bin/bash
set -e
for script in apply_active_parameter_set approve_recommendation_and_apply \
              rdp_apply_approved_recommendation rdp_approve_recommendation \
              rdp_rollback_active_parameter_set rdp_freeze_parameter_set \
              rdp_create_parameter_release rdp_run_release_cycle \
              rdp_update_decision_registry; do
    if .venv/Scripts/python.exe scripts/${script}.py; then
        echo "FAIL: ${script} did not exit 2"
        exit 1
    elif [ $? -eq 2 ]; then
        echo "OK: ${script} correctly disabled"
    fi
done
```

### 3.7 回滚

单独 `git checkout pre-rdp-hardening-v1 -- scripts/<name>.py` 可恢复任一脚本。

---

## 4. A-1 DB schema 硬化

### 4.1 背景

Claude 审查（Agent 3）确认：核心 governance 表约束严重不足——`active_parameter_sets.parameter_set_id` 无 FK、`recommendations` 无 `UQ(round_id, combo_key)` 防重复候选、各表 status/severity 字段均为无 CHECK 的 VARCHAR。这些缺失意味着代码层校验被绕过时 DB 不兜底。

### 4.2 现状（已确认）

当前 `rdp_models.py` 约束清单（摘）：

| 表 | 已有约束 | 缺失 |
|----|---------|------|
| `active_parameter_sets` | `UQ(family, timeframe)` | `parameter_set_id` 无 FK；无 status CHECK |
| `parameter_apply_history` | `UQ(operation_id)` + IX | `from/to_parameter_set_id` 无 FK；`operation_type` 无 CHECK |
| `parameter_sets` | `UQ(parameter_set_id)` + IX | `status` 无 CHECK |
| `recommendations` | `UQ(recommendation_id)` + IX | 无 `source_round_id` 字段；`status` 无 CHECK |
| `active_decisions` | `UQ(family, timeframe)` | `active_parameter_set_id` 无 FK |
| `parameter_releases` | `UQ(release_id)` + IX | `parameter_set_id` 无 FK；`apply_result/observation_status` 无 CHECK |
| `rollback_recommendations` | `UQ(release_id)` | `suggested_target_parameter_set_id` 无 FK；`severity` 无 CHECK |
| `observation_results` | `UQ(release_id)` | `status/recommendation` 无 CHECK |
| `release_effectiveness` | `UQ(release_id)` + `UQ(evaluation_id)` | `conclusion` 无 CHECK |

### 4.3 目标 schema（批次 A 后）

对应本 SOW §2 工程目标 #3：DB 约束兜底。

### 4.4 迁移分为 4 阶段

DB 迁移窗口期间允许**短暂只读**（用户已同意决策 #6）。预期窗口总计 ≤ 5 分钟，建议在北京时间 06:00-07:00 执行。

**阶段 4.4.1：数据清理与孤儿记录检查（只读，不影响服务）**
**阶段 4.4.2：加 FK（需短暂只读）**
**阶段 4.4.3：加 UQ（需短暂只读，但通常已满足）**
**阶段 4.4.4：加 CHECK（毫秒级，业务感知低）**

### 4.5 阶段 4.4.1 — 数据清理（执行前 24h 跑 dry-run）

**新文件**：`aats/data_platform/migrations/batch_a_01_orphan_report.sql`

```sql
-- 批次 A 迁移前置：孤儿记录检查（只读）
-- 输出 7 个检查点，若任一有结果必须先人工 ack

\echo '=== 1. active_parameter_sets 的 parameter_set_id 是否都存在于 parameter_sets ==='
SELECT a.family, a.timeframe, a.parameter_set_id
FROM governance.active_parameter_sets a
LEFT JOIN governance.parameter_sets p
  ON a.parameter_set_id = p.parameter_set_id
WHERE p.parameter_set_id IS NULL;

\echo '=== 2. parameter_apply_history 的 to_parameter_set_id 是否都存在 ==='
SELECT h.operation_id, h.family, h.timeframe, h.to_parameter_set_id
FROM governance.parameter_apply_history h
LEFT JOIN governance.parameter_sets p
  ON h.to_parameter_set_id = p.parameter_set_id
WHERE h.to_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo '=== 3. parameter_apply_history 的 from_parameter_set_id 是否都存在 ==='
SELECT h.operation_id, h.family, h.timeframe, h.from_parameter_set_id
FROM governance.parameter_apply_history h
LEFT JOIN governance.parameter_sets p
  ON h.from_parameter_set_id = p.parameter_set_id
WHERE h.from_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo '=== 4. parameter_releases 的 parameter_set_id 是否都存在 ==='
SELECT r.release_id, r.parameter_set_id
FROM governance.parameter_releases r
LEFT JOIN governance.parameter_sets p
  ON r.parameter_set_id = p.parameter_set_id
WHERE p.parameter_set_id IS NULL;

\echo '=== 5. rollback_recommendations 的 suggested_target_parameter_set_id ==='
SELECT r.release_id, r.suggested_target_parameter_set_id
FROM governance.rollback_recommendations r
LEFT JOIN governance.parameter_sets p
  ON r.suggested_target_parameter_set_id = p.parameter_set_id
WHERE r.suggested_target_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo '=== 6. active_decisions 的 active_parameter_set_id 是否都存在 ==='
SELECT d.family, d.timeframe, d.active_parameter_set_id
FROM governance.active_decisions d
LEFT JOIN governance.parameter_sets p
  ON d.active_parameter_set_id = p.parameter_set_id
WHERE d.active_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo '=== 7. 各表 status 字段异常值统计 ==='
SELECT 'recommendations' AS tbl, status, COUNT(*) FROM governance.recommendations
  GROUP BY status
UNION ALL
SELECT 'parameter_sets', status, COUNT(*) FROM governance.parameter_sets GROUP BY status
UNION ALL
SELECT 'parameter_releases.apply_result', apply_result, COUNT(*) FROM governance.parameter_releases GROUP BY apply_result
UNION ALL
SELECT 'parameter_releases.observation_status', observation_status, COUNT(*) FROM governance.parameter_releases GROUP BY observation_status;
```

**执行纪律**：迁移前 24h 跑一次，任一结果非空则人工审阅 → 补数据清理 SQL → 再跑到全空 → 才启动迁移窗口。

### 4.6 阶段 4.4.2 — 加 FK

**新文件**：`aats/data_platform/migrations/batch_a_02_add_fks.sql`

```sql
BEGIN;

-- FK 1: active_parameter_sets.parameter_set_id → parameter_sets
ALTER TABLE governance.active_parameter_sets
  ADD CONSTRAINT fk_active_ps_id
  FOREIGN KEY (parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

-- FK 2: parameter_apply_history.to_parameter_set_id
ALTER TABLE governance.parameter_apply_history
  ADD CONSTRAINT fk_apply_history_to_ps
  FOREIGN KEY (to_parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

-- FK 3: parameter_apply_history.from_parameter_set_id
ALTER TABLE governance.parameter_apply_history
  ADD CONSTRAINT fk_apply_history_from_ps
  FOREIGN KEY (from_parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

-- FK 4: parameter_releases.parameter_set_id
ALTER TABLE governance.parameter_releases
  ADD CONSTRAINT fk_param_release_ps
  FOREIGN KEY (parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

-- FK 5: parameter_releases.previous_parameter_set_id
ALTER TABLE governance.parameter_releases
  ADD CONSTRAINT fk_param_release_prev_ps
  FOREIGN KEY (previous_parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

-- FK 6: rollback_recommendations.suggested_target_parameter_set_id
ALTER TABLE governance.rollback_recommendations
  ADD CONSTRAINT fk_rollback_rec_target_ps
  FOREIGN KEY (suggested_target_parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

-- FK 7: active_decisions.active_parameter_set_id
ALTER TABLE governance.active_decisions
  ADD CONSTRAINT fk_active_decision_ps
  FOREIGN KEY (active_parameter_set_id)
  REFERENCES governance.parameter_sets(parameter_set_id)
  ON DELETE RESTRICT ON UPDATE RESTRICT;

COMMIT;
```

**重要说明**：
- 所有 FK 用 `ON DELETE RESTRICT` —— 禁止级联删除，parameter_sets 只能通过 deprecated 状态标记废弃
- 执行期间相关表短暂加表级锁（<5s），API 会收到 503；可接受

### 4.7 阶段 4.4.3 — 加 UQ

**新文件**：`aats/data_platform/migrations/batch_a_03_add_uqs.sql`

> **设计决策**：Recommendation 的"同 round 不允许多候选"约束需要一个 `source_round_id` 列。现有 `RecommendationModel` 没有这个字段，从 `target_parameter_set_id` 反查 `parameter_sets.source_round_id` 可以间接得到，但加列更清晰。本 SOW 选择**加列**。

```sql
BEGIN;

-- 新增 recommendations.source_round_id 列（允许 NULL 以兼容历史数据）
ALTER TABLE governance.recommendations
  ADD COLUMN source_round_id VARCHAR(128);

-- 回填：从 target_parameter_set_id → parameter_sets.source_round_id
UPDATE governance.recommendations r
SET source_round_id = p.source_round_id
FROM governance.parameter_sets p
WHERE r.target_parameter_set_id = p.parameter_set_id
  AND r.source_round_id IS NULL;

-- 部分唯一索引：同一 round + family + timeframe 仅允许一条非 superseded recommendation
-- （允许 superseded 的重复，因为 supersede 是正常流程）
CREATE UNIQUE INDEX uq_rec_round_family_tf_active
  ON governance.recommendations (source_round_id, family, timeframe)
  WHERE source_round_id IS NOT NULL
    AND status NOT IN ('superseded', 'rejected');

COMMIT;
```

### 4.8 阶段 4.4.4 — 加 CHECK

**新文件**：`aats/data_platform/migrations/batch_a_04_add_checks.sql`

```sql
BEGIN;

ALTER TABLE governance.parameter_sets
  ADD CONSTRAINT ck_ps_status
  CHECK (status IN ('draft', 'candidate', 'frozen', 'released', 'deprecated'));

ALTER TABLE governance.recommendations
  ADD CONSTRAINT ck_rec_status
  CHECK (status IN ('draft', 'approved', 'rejected', 'superseded', 'applied', 'rolled_back'));

ALTER TABLE governance.parameter_apply_history
  ADD CONSTRAINT ck_apply_op_type
  CHECK (operation_type IN ('apply', 'rollback'));

ALTER TABLE governance.parameter_releases
  ADD CONSTRAINT ck_release_apply_result
  CHECK (apply_result IN ('pending', 'success', 'failed', 'rolled_back'));

ALTER TABLE governance.parameter_releases
  ADD CONSTRAINT ck_release_observation_status
  CHECK (observation_status IN ('pending', 'observing', 'completed', 'rolled_back', 'rollback_recommended', 'blocked_at_gate'));

ALTER TABLE governance.observation_results
  ADD CONSTRAINT ck_obs_status
  CHECK (status IN ('pending', 'observing', 'completed', 'inconclusive'));

ALTER TABLE governance.observation_results
  ADD CONSTRAINT ck_obs_recommendation
  CHECK (recommendation IN ('hold', 'rollback', 'continue_observing', 'inconclusive'));

ALTER TABLE governance.rollback_recommendations
  ADD CONSTRAINT ck_rollback_severity
  CHECK (severity IN ('none', 'warn', 'recommended', 'urgent', 'rejected'));

ALTER TABLE governance.release_effectiveness
  ADD CONSTRAINT ck_release_eff_conclusion
  CHECK (conclusion IN ('pending', 'positive', 'neutral', 'negative', 'rolled_back'));

COMMIT;
```

### 4.9 迁移 runner 集成

**变更文件**：`aats/data_platform/db.py`

在 `run_migrations()` 之外新增 `apply_batch_a_migrations()`：

```python
def apply_batch_a_migrations(
    settings: ResearchPlatformSettings | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """执行批次 A DB 硬化迁移。

    按 4 阶段顺序：01_orphan_report（dry_run 退出）→ 02_add_fks → 03_add_uqs → 04_add_checks。
    每阶段 idempotent——已存在的约束 ADD 会被 try/except 吞并记录到报告。
    """
    import pathlib
    migrations_dir = pathlib.Path(__file__).parent / "migrations"
    files = [
        "batch_a_01_orphan_report.sql",
        "batch_a_02_add_fks.sql",
        "batch_a_03_add_uqs.sql",
        "batch_a_04_add_checks.sql",
    ]
    report = {"dry_run": dry_run, "stages": []}
    engine = get_engine(settings)

    for f in files:
        sql = (migrations_dir / f).read_text(encoding="utf-8")
        if dry_run and f != "batch_a_01_orphan_report.sql":
            report["stages"].append({"file": f, "skipped": "dry_run"})
            continue
        # 执行 sql，捕获每个 DDL 的结果
        with engine.begin() as conn:
            # ... 逐语句执行 + 容错已存在约束 ...
        report["stages"].append({"file": f, "status": "ok"})
    return report
```

**新增脚本**：`scripts/rdp_run_batch_a_migration.py`

```python
#!/usr/bin/env python3
"""Execute batch A DB hardening migration.

用法:
  .venv\\Scripts\\python.exe scripts/rdp_run_batch_a_migration.py --dry-run
  .venv\\Scripts\\python.exe scripts/rdp_run_batch_a_migration.py --confirm-prod
"""
# dry-run 先跑；确认报告无异常后 --confirm-prod 实际执行
```

### 4.10 ORM 同步更新

`aats/data_platform/rdp_models.py` 需同步加入约束（避免 `create_all` 在新环境生成无约束 schema）：

每张表的 `__table_args__` 补入：

```python
# ActiveParameterSetModel
__table_args__ = (
    UniqueConstraint("family", "timeframe", name="uq_active_combo"),
    ForeignKeyConstraint(
        ["parameter_set_id"],
        ["governance.parameter_sets.parameter_set_id"],
        name="fk_active_ps_id",
        ondelete="RESTRICT",
    ),
    {"schema": "governance"},
)

# 其他 6 张表类似 + CHECK 约束
```

### 4.11 回滚 SQL（灾难应急）

**新文件**：`aats/data_platform/migrations/batch_a_99_rollback.sql`

```sql
-- 批次 A 迁移灾难回滚——仅在事故应急时执行
BEGIN;

-- CHECK
ALTER TABLE governance.parameter_sets DROP CONSTRAINT IF EXISTS ck_ps_status;
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS ck_rec_status;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS ck_apply_op_type;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS ck_release_apply_result;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS ck_release_observation_status;
ALTER TABLE governance.observation_results DROP CONSTRAINT IF EXISTS ck_obs_status;
ALTER TABLE governance.observation_results DROP CONSTRAINT IF EXISTS ck_obs_recommendation;
ALTER TABLE governance.rollback_recommendations DROP CONSTRAINT IF EXISTS ck_rollback_severity;
ALTER TABLE governance.release_effectiveness DROP CONSTRAINT IF EXISTS ck_release_eff_conclusion;

-- UQ
DROP INDEX IF EXISTS governance.uq_rec_round_family_tf_active;
ALTER TABLE governance.recommendations DROP COLUMN IF EXISTS source_round_id;

-- FK
ALTER TABLE governance.active_parameter_sets DROP CONSTRAINT IF EXISTS fk_active_ps_id;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS fk_apply_history_to_ps;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS fk_apply_history_from_ps;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS fk_param_release_ps;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS fk_param_release_prev_ps;
ALTER TABLE governance.rollback_recommendations DROP CONSTRAINT IF EXISTS fk_rollback_rec_target_ps;
ALTER TABLE governance.active_decisions DROP CONSTRAINT IF EXISTS fk_active_decision_ps;

COMMIT;
```

### 4.12 测试

**集成测试**（`tests/integration/test_rdp_batch_a_db_constraints.py`，testcontainers）：

```python
def test_fk_blocks_active_with_nonexistent_ps()
def test_fk_blocks_apply_history_with_nonexistent_ps()
def test_fk_blocks_release_with_nonexistent_ps()
def test_fk_cascade_restrict_prevents_ps_delete_when_active()
def test_uq_blocks_duplicate_recommendation_in_same_round()
def test_uq_allows_superseded_recommendation_retry()
def test_check_rejects_illegal_status_values()
def test_check_rejects_illegal_observation_status()
def test_migration_is_idempotent()  # 跑 2 次不报错
def test_rollback_migration_restores_pre_state()
```

---

## 5. A-0.3 清扫 "DB → JSON 成功" 反模式

### 5.1 背景

用户 P0-4 + Claude 深挖：`_db_update_rec_status` 等函数在 DB 失败时返回 `None`，调用方仅把 `False`（CAS 冲突）当失败，`None` 则继续走 JSON 路径"成功"，造成 split-brain。同款模式在至少 4 处。

### 5.2 现状（证据）

[recommendation_registry.py:75-120](../../aats/data_platform/decision_system/recommendation_registry.py)：

```python
def _db_update_rec_status(rec, expected_current_status) -> Optional[bool]:
    engine, ok = try_governance_db()
    if not ok:
        return None  # ← DB 不可达
    try:
        return db_update_recommendation_status(...)
    except Exception as exc:
        log.warning("...: DB 状态更新失败 (%s)", exc)
        return None  # ← DB 查询异常
```

[recommendation_registry.py:384-408](../../aats/data_platform/decision_system/recommendation_registry.py)：

```python
db_result = _db_update_rec_status(rec, expected_current_status="draft")
if db_result is False:  # 只有 False 时才回滚！
    for key, value in prev_snapshot.items():
        rec[key] = value
    return None
# 若 db_result = None，直接跳过此块，返回已修改的 rec
return rec
```

同款在 `release_registry.py:139`、`active_decision_registry.py`、`evidence_bundle_index.py`。

### 5.3 目标状态

所有 `_db_update_*` 函数废除"None = DB 不可用"的模糊语义，改为抛 `DBUnavailableError`（新异常类）。调用方必须显式处理：
- 在事务入口捕获并返回 5xx 给 API
- 在后台任务中捕获并写入 failure_registry + 退出重试循环

**硬纪律**：文件只在 DB 成功后作为审计副本写入；DB 失败 → 文件不写 → API 返回错误。

### 5.4 变更清单

#### 5.4.1 新增异常类

**新文件**：`aats/data_platform/governance/_exceptions.py`

```python
class GovernanceDBError(Exception):
    """所有 governance DB 操作失败的基类。"""

class DBUnavailableError(GovernanceDBError):
    """DB 连接不可用（连接失败、超时等基础设施问题）。"""

class DBConstraintViolation(GovernanceDBError):
    """DB 约束违反（FK/UQ/CHECK），通常是业务逻辑错误。"""

class DBConflictError(GovernanceDBError):
    """CAS 冲突，调用方应读最新状态后重试或放弃。"""
```

#### 5.4.2 改写 `_db_update_rec_status`

**文件**：`recommendation_registry.py`，函数 `_db_update_rec_status`

```python
def _db_update_rec_status(rec, expected_current_status) -> bool:
    """返回 True = CAS 成功；False = CAS 失败（别的进程改了）；
    抛 DBUnavailableError = DB 基础设施问题；
    抛 DBConstraintViolation = FK/CHECK 违反。
    """
    engine, ok = try_governance_db()
    if not ok:
        raise DBUnavailableError("governance DB not reachable")
    try:
        return db_update_recommendation_status(...)
    except IntegrityError as exc:
        raise DBConstraintViolation(str(exc)) from exc
    except OperationalError as exc:
        raise DBUnavailableError(str(exc)) from exc
```

#### 5.4.3 改写调用方

**文件**：同上，函数 `approve_recommendation`

```python
try:
    db_result = _db_update_rec_status(rec, expected_current_status="draft")
except DBUnavailableError:
    # 不再降级；回滚内存状态；调用方（API 层）返回 503
    for key, value in prev_snapshot.items():
        rec[key] = value
    raise  # 交给 API handler 转 5xx

if db_result is False:  # CAS 冲突
    for key, value in prev_snapshot.items():
        rec[key] = value
    log.warning("approve: CAS 冲突")
    return None

# DB 成功，此时才写文件审计副本
_write_file_audit(...)
return rec
```

#### 5.4.4 同款处理其他 3 处

| 文件 | 函数 | 处理 |
|------|------|------|
| `release_registry.py:139` 附近 | `_db_upsert_release` | 按 §5.4.2/5.4.3 模式改写 |
| `active_decision_registry.py` | `_db_update_active_decision` | 同上 |
| `evidence_bundle_index.py` | `_db_upsert_evidence` | 同上 |

#### 5.4.5 API handler 异常映射

**文件**：`aats/api/rdp_routes.py`

在路由顶层加统一 exception handler：

```python
@app.exception_handler(DBUnavailableError)
async def _db_unavailable_handler(request, exc):
    log.error("DB unavailable on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"error": "db_unavailable", "detail": str(exc)}
    )

@app.exception_handler(DBConstraintViolation)
async def _db_constraint_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"error": "db_constraint_violation", "detail": str(exc)}
    )
```

### 5.5 测试

**集成测试**（`tests/integration/test_rdp_no_db_fallback.py`）：

```python
def test_approve_returns_503_when_db_unreachable():
    # 使用 testcontainers 启动 DB，然后 docker stop
    # 调用 approve API，应返回 503
    # 验证 JSON 文件未被写入

def test_release_create_returns_503_when_db_unreachable()
def test_active_decision_update_returns_503_when_db_unreachable()
def test_evidence_bundle_write_returns_503_when_db_unreachable()

def test_json_audit_written_only_after_db_success()
    # 造 DB success + 文件写失败 的场景
    # 确认：API 返回 200，文件警告已记录，但 DB 状态正确
```

### 5.6 回滚

单独 revert 本项对应 commit。API 层 exception handler 回到不处理（默认 500）。

---

## 6. A-0.4 Gate ISO 时间解析统一

### 6.1 背景

近期 commit `fix: tighten rdp governance hot paths` 修了 `gate_rules.py` 的 tzinfo-naive 导致 `TypeError` 被吞掉、`age_hours` 比较被绕过的 P0 bug。Claude 审查指出同款模式在 `gate_runtime_contract._parse_iso_datetime` 仍在，未来还会复发。

### 6.2 目标

抽出公共工具 `parse_iso_datetime_utc`，强制 tz-aware 返回；所有 ISO 时间解析点统一调用。

### 6.3 变更清单

#### 6.3.1 新增工具

**新文件**：`aats/data_platform/governance/_time_util.py`

```python
"""统一的 ISO 时间解析工具。

所有 governance 模块解析 ISO 字符串必须用这个函数，不允许直接 datetime.fromisoformat()
或本地 _parse_iso_datetime helper。

设计原则：
- 输入 None / 空串 → 返回 None（调用方决定如何处理缺失）
- 输入非法格式 → 抛 ValueError（不静默吞）
- 输入 naive 字符串 → 视为 UTC（加 tzinfo），不抛错但记 WARN log
- 输入 tz-aware → 转换为 UTC
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
import logging

log = logging.getLogger(__name__)


def parse_iso_datetime_utc(value: Optional[str], *, context: str = "") -> Optional[datetime]:
    if value is None or not str(value).strip():
        return None
    s = str(value).strip()
    # 兼容 'Z' 后缀
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"illegal_iso_datetime:{value!r} at {context}")
    if dt.tzinfo is None:
        log.warning("parse_iso_datetime_utc: naive datetime %r at %s — assuming UTC", value, context)
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt
```

#### 6.3.2 替换所有调用点

**命令**：

```bash
grep -rn "fromisoformat\|_parse_iso_datetime" aats/data_platform/ aats/api/ aats/services/
```

预期命中点（基于审查）：
- `gate_rules.py:111`
- `gate_runtime_contract.py::_parse_iso_datetime`
- `observation_window.py::_check_evidence_freshness` 等
- `rdp_queries.py:_parse_iso_datetime`
- `rollback_policy.py` 相关
- 其他可能散落点（执行时 grep 确认）

全部改为 `from aats.data_platform.governance._time_util import parse_iso_datetime_utc`。

### 6.4 测试

**单元测试**（`tests/unit/test_rdp_time_util.py`）：

```python
def test_parse_none_returns_none()
def test_parse_empty_returns_none()
def test_parse_illegal_raises()
def test_parse_naive_returns_utc_with_warning()
def test_parse_z_suffix()
def test_parse_offset_converts_to_utc()
def test_idempotent_when_already_utc()
```

---

## 7. A-0.5 `RDP_PRODUCTION_APPLY_ENABLED` → short-lived token

### 7.1 背景

用户 P1 + Agent 2：脚本无认证，`RDP_PRODUCTION_APPLY_ENABLED` env flag 是唯一屏障，易被 CI/CD 镜像层泄露。用户决策（#3）：废弃此 flag，改为绑定操作员身份的 short-lived token。

### 7.2 目标

- 环境变量 `RDP_PRODUCTION_APPLY_ENABLED` 完全删除
- 新增 `POST /rdp/operator-tokens` API：操作员登录 session 下可签发 TTL=300s 的 HMAC token
- apply/rollback API 要求 header `X-Rdp-Apply-Token: <token>`；未提供或过期返回 403
- 新增 CLI `scripts/rdp_emit_apply_token.py` 用于紧急运维通道（绑定当前 shell 环境的 operator 身份）

### 7.3 变更清单

#### 7.3.1 Token 签发与校验

**新文件**：`aats/api/rdp_apply_token.py`

```python
"""RDP apply/rollback 的 short-lived token 机制。

设计：
- 签发：HMAC(secret, f"{actor}|{action}|{exp_ts}"), 返回 base64(exp_ts.actor.action.sig)
- TTL：默认 300s；可通过环境 RDP_APPLY_TOKEN_TTL_SECONDS 覆盖（下限 60, 上限 900）
- Secret：从 settings 读取（新增 RDP_APPLY_TOKEN_SECRET 环境变量，必须存在）
- 校验：解包 → 校 sig → 校 exp_ts → 返回 (actor, action) 或抛 InvalidTokenError
"""
from __future__ import annotations
import base64, hashlib, hmac, os, time
from typing import Tuple


class InvalidTokenError(Exception):
    pass


def _secret() -> bytes:
    secret = os.environ.get("RDP_APPLY_TOKEN_SECRET")
    if not secret:
        raise RuntimeError("RDP_APPLY_TOKEN_SECRET not configured")
    return secret.encode("utf-8")


def _ttl() -> int:
    raw = int(os.environ.get("RDP_APPLY_TOKEN_TTL_SECONDS", "300"))
    return max(60, min(raw, 900))


def emit_token(actor: str, action: str) -> str:
    assert action in ("apply", "rollback", "freeze"), action
    exp_ts = int(time.time()) + _ttl()
    payload = f"{actor}|{action}|{exp_ts}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def verify_token(token: str, required_action: str) -> Tuple[str, int]:
    """返回 (actor, exp_ts)；抛 InvalidTokenError 如果校验失败。"""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        actor, action, exp_ts_str, sig = raw.split("|")
    except Exception:
        raise InvalidTokenError("malformed")
    if action != required_action:
        raise InvalidTokenError(f"action_mismatch:expected={required_action} got={action}")
    try:
        exp_ts = int(exp_ts_str)
    except ValueError:
        raise InvalidTokenError("invalid_exp")
    if exp_ts < int(time.time()):
        raise InvalidTokenError("expired")
    expected_sig = hmac.new(_secret(), f"{actor}|{action}|{exp_ts}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise InvalidTokenError("bad_sig")
    return actor, exp_ts
```

#### 7.3.2 API 签发端点

**文件**：`aats/api/rdp_routes.py`
**新增**：

```python
@router.post("/rdp/operator-tokens")
async def emit_operator_token(
    req: EmitTokenRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
):
    """签发一次性 apply/rollback/freeze token。要求操作员 session 登录。"""
    if req.action not in ("apply", "rollback", "freeze"):
        raise HTTPException(422, "invalid_action")
    token = emit_token(actor=principal.identity, action=req.action)
    return {"token": token, "ttl_seconds": _ttl(), "action": req.action, "actor": principal.identity}
```

#### 7.3.3 API 消费端校验

**文件**：同上，apply/rollback/freeze 路由增加 header 依赖

```python
def _require_apply_token(required_action: str):
    def _dep(request: Request, x_rdp_apply_token: str = Header(None)):
        if x_rdp_apply_token is None:
            raise HTTPException(403, "missing_apply_token")
        try:
            actor, exp_ts = verify_token(x_rdp_apply_token, required_action)
        except InvalidTokenError as exc:
            raise HTTPException(403, f"invalid_token:{exc}")
        request.state.token_actor = actor
        return actor
    return _dep

# 应用
@router.post("/rdp/parameters/apply")
async def apply_parameters(
    req: ApplyRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
    token_actor: str = Depends(_require_apply_token("apply")),
):
    # 双重校验：session 和 token 的 actor 必须一致
    if principal.identity != token_actor:
        raise HTTPException(403, "actor_mismatch")
    ...
```

#### 7.3.4 CLI 签发工具

**新文件**：`scripts/rdp_emit_apply_token.py`

```python
#!/usr/bin/env python3
"""紧急运维通道：签发一次性 apply/rollback token。

用法:
  .venv\\Scripts\\python.exe scripts/rdp_emit_apply_token.py --actor <operator> --action apply

生产环境必须由运维本人执行，不能放入 crontab/CI。
"""
import argparse, os, sys
from aats.api.rdp_apply_token import emit_token, _ttl

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--actor", required=True)
    p.add_argument("--action", required=True, choices=["apply", "rollback", "freeze"])
    args = p.parse_args()
    if not os.environ.get("RDP_APPLY_TOKEN_SECRET"):
        print("error: RDP_APPLY_TOKEN_SECRET not set", file=sys.stderr)
        sys.exit(2)
    token = emit_token(args.actor, args.action)
    print(f"Token (TTL={_ttl()}s) for {args.actor}/{args.action}:")
    print(token)
    print("")
    print("Usage:")
    print(f"  curl -H 'X-Rdp-Apply-Token: {token}' ...")

if __name__ == "__main__":
    main()
```

#### 7.3.5 清理旧 flag

- `.env.derivatives.live` 中 `RDP_PRODUCTION_APPLY_ENABLED` 行删除
- `aats/` 下搜索该变量名，每个引用点判断：
  - 若是"有/无此 flag 改变行为"的 if 分支，改为无条件走新路径
  - 若是 UI 展示 flag 状态，改为展示 token 签发历史
- 新增 `RDP_APPLY_TOKEN_SECRET` 环境变量在 `.env.derivatives.live`（不提交到 git，通过 WSL2 部署脚本注入）

### 7.4 测试

**单元测试**（`tests/unit/test_rdp_apply_token.py`）：

```python
def test_emit_and_verify_ok()
def test_expired_token_rejected()
def test_wrong_action_rejected()
def test_tampered_sig_rejected()
def test_malformed_token_rejected()
def test_ttl_clamped_to_bounds()
```

**集成测试**（`tests/integration/test_rdp_apply_api_with_token.py`）：

```python
def test_apply_without_token_returns_403()
def test_apply_with_expired_token_returns_403()
def test_apply_with_token_of_different_actor_returns_403()
def test_apply_with_valid_token_returns_200()
```

---

## 8. 执行顺序与时间预估

### 8.1 推荐执行时序

```
D1 上午  A-1 阶段 4.4.1 数据清理 dry-run（不动 DB）
D1 下午  数据清理审阅 + 若有孤儿记录补 cleanup SQL → 再跑 dry-run 到全空
D2 上午  A-0.4 时区统一（影响面小，先做热身）+ 单元测试
D2 下午  A-0.3 DB 降级清扫（4 处改写）+ 集成测试
D3 上午  A-1 阶段 4.4.2/3/4 真实迁移（06:00-07:00 窗口）+ 集成测试
D3 下午  A-0.1 Rollback 收口（依赖 D3 上午的 FK）+ 单元/集成测试
D4 上午  A-0.5 Token 机制 + 单元/集成测试
D4 下午  A-0.2 Legacy 脚本禁用 + 冒烟测试；清扫 grep 守门
D5 上午  端到端冒烟：API 完整路径；wsl2 集成测试全套
D5 下午  写批次 A 收尾报告；评估是否可启动批次 B 详设
```

### 8.2 并行机会

- A-0.4 与 A-0.5 完全独立可并行
- A-1 阶段 4.4.1 dry-run 可与 A-0.4/A-0.5/A-0.3 并行（dry-run 不动 DB）
- A-0.2 必须最后做（先做会阻塞其他脚本依赖）

---

## 9. 验收 checklist（与 SOW §6.1 一致 + 详细展开）

### 9.1 代码验收

- [ ] `grep -rn "active_parameter_sets" aats/ scripts/ | grep -iE "update|insert"` 仅在 `active_parameter_apply.py` 和 DB 迁移文件中
- [ ] `grep -rn "RDP_PRODUCTION_APPLY_ENABLED" aats/ scripts/ .env*` 零命中
- [ ] `grep -rn "bypassed_frozen\|apply-frozen\|action_apply_frozen" aats/ scripts/` 零命中
- [ ] `grep -rn "skip_gate" aats/ scripts/` 剩余命中必须都在测试代码（`tests/**`）内且意图是"测试 gate 强制"
- [ ] `grep -rn "fromisoformat" aats/data_platform/ aats/api/ aats/services/` 仅在 `_time_util.py` 中

### 9.2 CI 守门（持久化）

在 `.github/workflows/` 或项目 `Makefile`/`scripts/precommit.sh` 中增加：

```bash
# 禁止新 PR 引入这些字符串
forbidden_patterns=(
    "bypassed_frozen"
    "apply-frozen"
    "RDP_PRODUCTION_APPLY_ENABLED"
    "skip_gate=True"
)
for p in "${forbidden_patterns[@]}"; do
    if git diff --cached | grep -q "$p"; then
        echo "FORBIDDEN: staged diff contains '$p'"
        exit 1
    fi
done
```

### 9.3 DB 验收

- [ ] `\d governance.active_parameter_sets` 显示 `fk_active_ps_id` FK
- [ ] `\d governance.parameter_apply_history` 显示 `fk_apply_history_to_ps` 和 `fk_apply_history_from_ps` FK
- [ ] `\d governance.parameter_releases` 显示 `fk_param_release_ps` 和 `fk_param_release_prev_ps` FK
- [ ] `\d governance.rollback_recommendations` 显示 `fk_rollback_rec_target_ps` FK
- [ ] `\d governance.active_decisions` 显示 `fk_active_decision_ps` FK
- [ ] `\d governance.recommendations` 显示 `source_round_id` 列 + `uq_rec_round_family_tf_active` 部分唯一索引
- [ ] 各表 `status/severity/conclusion` 字段有 CHECK 约束

### 9.4 测试通过

- [ ] `tests/unit/test_rdp_*.py` 全通过（新增约 25 个用例 + 原有）
- [ ] `tests/integration/test_rdp_batch_a_*.py` 在 WSL2 testcontainers 全通过（约 15 个）
- [ ] `tests/smoke/test_rdp_legacy_scripts_disabled.sh` 全通过
- [ ] 全套 pytest 套件无新增 regression

### 9.5 运维验收

- [ ] 执行 `scripts/rdp_run_batch_a_migration.py --dry-run`，orphan 报告清洁
- [ ] 实盘环境（模拟盘）`curl -X POST /rdp/parameters/apply` 无 token → 403
- [ ] 签发 token + `curl` → 成功 apply + `active_parameter_sets` 真实更新
- [ ] 故意构造 rollback 到非法 id → 422 + `rollback_recommendations` 写入拒绝记录
- [ ] `docker compose stop aats-postgres` → `curl /rdp/recommendations/.../approve` → 503 + JSON 未变更

---

## 10. 风险与应急

| 风险 | 缓解 |
|------|------|
| DB 迁移窗口超时（>5 分钟 API 503）| 分 4 阶段执行；每阶段独立 commit；发现慢则回滚本阶段，下一窗口再试 |
| Token secret 遗失导致全面 403 | `.env.derivatives.live` 在 `D:\文件\芝麻开门\` 备份凭证；secret 有轮换流程 |
| 操作员忘记获取 token | API 错误响应明示步骤：`POST /rdp/operator-tokens` 或 `scripts/rdp_emit_apply_token.py` |
| 脚本 stub 误伤合法运维流程 | 开工前 24h 扫 `crontab -l` 与 WSL2 的 systemd timers；发现调用联系用户 |
| 孤儿记录超预期需要大量数据清理 | 若 orphan_report 输出超过 50 行，暂停迁移，先做数据清理 spike |

---

## 11. 审批签字

- [ ] 用户审阅本详设
- [ ] 用户确认 6 个决策点已完整体现（本文档 §3-§7）
- [ ] 用户确认执行时序（§8）可接受
- [ ] 用户明确授权开工 D1（建议日期：______）

开工前 Claude 再次确认：
- [ ] pre-rdp-hardening-v1 tag 存在（已确认）
- [ ] `docs/task/rdp_full_hardening_sow.md` 和本文档均已提交 git（待提交）
