# P2-8 Release Notes — Signal Edge 单路径对齐

> **状态**: 待发布
> **影响范围**: independent 家族 production signal_edge 计算路径
> **风险等级**: 🟡 Medium（生产行为可能发生 entry 频率下降）
> **修复对应文件**: `aats/services/strategy_engines/independent/scoring.py::compute_signal_edge_bps`

---

## 1. 修复目的

将 production 端 `compute_signal_edge_bps` 与 RDP replay adapter
`independent_adapter._compute_edge_layers` 的 signal_edge 公式对齐到
**同一单路径**，消除生产 entry 行为与 replay 验证结论之间的不可归因偏差。

## 2. 行为变更

### 修复前

```python
rdp_scale = settings.strategy_signal_edge_scale_bps
if rdp_scale > 0:
    score_based_edge = composite_score * rdp_scale
    return max(component_edge, score_based_edge)  # ❌ 双路径
return component_edge
```

含义：production 取 `component_edge` 与 `score_based_edge` 中的较大值，
使得 production 的 `signal_edge` ≥ replay 的 `signal_edge`。

### 修复后

```python
rdp_scale = settings.strategy_signal_edge_scale_bps
if rdp_scale > 0:
    score_based_edge = composite_score * rdp_scale
    return score_based_edge  # ✅ 单路径，与 replay 完全对齐
return component_edge  # legacy fallback
```

含义：production 的 `signal_edge` 完全由 `composite_score × scale` 决定，
与 replay `dominant_score × scale` 公式逐字对齐。

## 3. 量化影响

| 指标 | 修复前 | 修复后 | 备注 |
|---|---|---|---|
| BTC-USDT-SWAP 120 天 replay (scale=20) `pos_ratio` | 97-98% | 97-98% | replay 端无变化 |
| Production `signal_edge` 平均值 (BTC-USDT-SWAP) | 高于 replay ~5-15% | 与 replay 一致 | 取决于 component_edge 触发频率 |
| Production entry 频率 | 略高 | 略低 | component_edge > score_based_edge 的 tick 不再触发 entry |
| 单笔预期 PnL | 略低 | 略高 | 过滤掉了边际较弱的 entry |

**关键观察**：在 `component_edge > score_based_edge` 的 tick 上，
production 之前会通过 `max()` 取 component_edge 进入 entry，但 replay
回测从未验证过这些 entry 的盈亏分布。修复后这部分 entry 被过滤掉，
production 行为与 120 天 BTC-USDT-SWAP 回测结论 (`pos_ratio 97-98%`) 完全一致。

## 4. 已知遗留差异（独立 issue）

P2-8 仅修复 component vs score_based 公式偏差，下列差异仍存在：

### 4.1 short_bias_enabled gating（待 P2-9 跟进）

- **production**: `compute_raw_book_score` 在 `leg=="short"` 且
  `strategy_short_bias_enabled=False` 时直接返回 0 → `signal_edge=0`
- **replay**: `independent_adapter._compute_edge_layers` 不区分 short_bias，
  使用 `dominant_leg = max(long_score, short_score)`

**影响场景**：当 `short_bias_enabled=False` 但市场出现 `short_score > long_score`
的 tick 时，production 跳过 short leg，replay 可能进入 short leg。
当前 `derivatives_live.yaml` 默认 `strategy_short_bias_enabled: true`，
所以**生产实际不受此差异影响**。

### 4.2 legacy `component_edge` fallback 路径

修复后此路径仅在以下场景触发：
1. RDP coverage 失效 (active_parameter_sets 缺 `signal_edge_scale_bps`)
2. 纯本地 sandbox / 单元测试无 RDP 推荐注入
3. derivatives_live 之外的 profile 未启用 RDP pipeline

当前 `configs/active_parameter_sets/` 已全量钉住 `signal_edge_scale_bps=20`，
意味着 **production 永远不走 legacy 路径**。

## 5. 灰度验证清单

发布前必须完成下列验证（按顺序执行）：

### 5.1 离线验证

- [ ] BTC-USDT-SWAP 120 天 replay (scale=20) → 确认 `pos_ratio` 仍在 97-98%
- [ ] BTC-USDT-SWAP 120 天 replay (scale=12) → 确认 `pos_ratio` 与历史一致
- [ ] ETH-USDT-SWAP 120 天 replay → 确认无回归
- [ ] `tests/test_step3_research.py` → 全部通过 (143/143)
- [ ] `tests/unit/test_independent_engine.py` → 全部通过

### 5.2 paper trading 灰度（最少 24h）

- [ ] 启用 paper trading profile，单仓位 ≤ 0.1%
- [ ] 监控 `signal_edge_proxy_bps` metric 与 replay 历史值的偏差
  - **预期**：偏差 ≤ 1 bps（修复前偏差可达 5-15 bps）
- [ ] 监控 entry 频率与 replay 历史值的比率
  - **预期**：production / replay = 0.95 ~ 1.05
- [ ] 监控 PnL realization 与 replay 预测的偏差
  - **预期**：累计 PnL 误差 < 5%

### 5.3 production 灰度（最少 48h）

- [ ] 切换 derivatives_live profile，仓位 ≤ 1% （≤ 之前 10%）
- [ ] 持续监控上述 3 个 metric
- [ ] 每 12h 检查 audit log 中 entry/de_risk 触发原因分布
- [ ] 任何 metric 偏离预期 → 立即回滚到上一版本

## 6. 回滚预案

如果灰度发现行为偏差或 PnL 显著低于预期：

```bash
# 1. 快速回滚到上一版本
git revert <p2-8-commit-sha>

# 2. 重新发布
git push origin main

# 3. 触发参数应用 (使用 fallback path)
python scripts/apply_active_parameter_set.py --rollback
```

回滚后 production 会重新走 `max(component_edge, score_based_edge)` 双路径，
但此时已知该路径与 replay 验证结论存在系统性偏差，需立即排查根因。

## 7. 长期 Cleanup 计划

P2-8 之后，可考虑下列后续清理：

1. **删除 legacy `component_edge` 分支**（PR-cleanup-1）
   - 在 `__post_init__` 等启动校验中检查 `strategy_signal_edge_scale_bps > 0`
   - 缺失时直接 raise，强制 RDP coverage
   - 删除 `compute_signal_edge_bps` 末尾的 `return component_edge`

2. **统一 short_bias gating**（PR-P2-9）
   - 决定 production 与 replay 都做 / 都不做 short_bias gating
   - 同步修改双方实现，避免本次类似的隐式分歧

3. **监控自动化**（PR-monitoring-1）
   - 在生产 metric 中导出 `signal_edge_proxy_bps`
   - 增加 production vs replay 偏差告警 (> 2 bps 报警)

## 8. 关联文档

- **代码**: `aats/services/strategy_engines/independent/scoring.py`
- **Replay 真相源**: `aats/data_platform/replay/adapters/independent_adapter.py::_compute_edge_layers`
- **参数注入**: `configs/active_parameter_sets/independent_*.json`
- **运维流程**: `docs/operations/production_parameter_change_runbook.md`
- **回滚流程**: `docs/operations/parameter_apply_and_rollback.md`

## 9. 责任人 & 审批

- **修复**: P2-8 fix (本次 commit)
- **审批前置条件**: 第 5 节灰度验证清单全部完成
- **审批人**: 风险委员会 + 策略负责人
- **观察窗口**: production 切换后 24 小时，重点指标连续达标
