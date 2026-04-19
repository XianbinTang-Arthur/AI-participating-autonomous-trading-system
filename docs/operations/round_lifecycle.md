# Round 生命周期 (Round Lifecycle)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 状态定义

每个 round / run 统一使用以下状态:

```
pending -> running -> succeeded
                   \-> partial_success
                   \-> failed
                                    \-> deprecated
```

| 状态 | 含义 | 退出码 |
|------|------|--------|
| `pending` | 已创建，未开始 | - |
| `running` | 正在执行 | - |
| `succeeded` | 全部 combo 成功 | 0 |
| `partial_success` | 部分 combo 成功 | 2 |
| `failed` | 全部 combo 失败 | 3 |
| `deprecated` | 已废弃（手动标记） | - |

---

## 2. 退出码语义

### 2.1 One-shot 脚本
- `0` = 成功
- `2` = 部分成功（如 replay 正常但无 bar 数据匹配）
- 其他 = 失败

### 2.2 Round Runner
- `0` = 所有 combo succeeded 或 partial_success
- `2` = 有 combo failed，但至少有 1 个 succeeded/partial
- `3` = 全部 combo failed

---

## 3. Combo 级别状态

每个 round 包含 4 个 combo (family × timeframe)。
每个 combo 独立有自己的状态:

```json
"combos": [
  {"key": "independent_15m", "status": "succeeded"},
  {"key": "independent_1h", "status": "failed"},
  {"key": "directional_15m", "status": "succeeded"},
  {"key": "directional_1h", "status": "partial_success"}
]
```

Round 整体状态由 combo 状态聚合:
- 全部 `succeeded` -> round `succeeded`
- 有 `failed` 且有 `succeeded/partial` -> round `partial_success`
- 全部 `failed` -> round `failed`

---

## 4. Active Round Index

路径: `artifacts/governance/active_round_index.json`

记录:
- 所有非 deprecated 的 round
- 每个 phase 的最新 round
- 状态分布统计

### 4.1 构建

```bash
python scripts/rdp_list_active_rounds.py
```

### 4.2 结构

```json
{
  "generated_at": "...",
  "summary": {
    "total_rounds": 5,
    "status_distribution": {"succeeded": 3, "failed": 1, "partial_success": 1},
    "phases_with_rounds": ["phase3", "phase4"]
  },
  "latest_by_phase": {
    "phase3": {"round_id": "...", "status": "succeeded", ...},
    "phase4": {"round_id": "...", "status": "partial_success", ...}
  },
  "all_rounds": [...]
}
```

---

## 5. 失败处理

### 5.1 发现失败

```bash
# 方式 1: 查看 active round index
python scripts/rdp_list_active_rounds.py --status failed

# 方式 2: 查看可重跑列表
python scripts/rdp_retry_failed_round.py --action list
```

### 5.2 诊断

1. 查看 round 目录下的 `round_manifest.json`
2. 找到 `status: "failed"` 的 combo
3. 查看对应 combo 的 run_dir（如果存在）
4. 检查 stderr 日志

### 5.3 重跑

```bash
# 生成重跑计划
python scripts/rdp_retry_failed_round.py --action plan \
    --round-dir <path> --phase <phase>

# 执行整轮重跑（会创建新 round）
python scripts/rdp_retry_failed_round.py --action rerun \
    --round-dir <path> --phase <phase>
```

注意: 重跑会创建**新的 round**，不会覆盖旧 round。旧 round 仍可追溯。

---

## 6. 废弃

当一个 round 被新 round 替代后，可手动标记为 deprecated:
- 目前通过直接编辑 `round_manifest.json` 设置 `"status": "deprecated"`
- 或在构建 active index 时使用 `--include-deprecated` 查看
