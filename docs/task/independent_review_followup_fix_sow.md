# Independent Review Follow-up Fix SoW

## Business objective
- 修复本轮代码审查暴露的遗留边界，确保 independent full-close 在 expectancy 缺失时仍保留真实退出 notional，并继续保持 operator 试盘守护摘要与决策层的 guarded/raw fallback 口径一致。

## Boundaries
- 仅修改 independent leg 构建逻辑与 operator query summary。
- 不调整 allocator 预算语义，不改动 live 参数，不扩展数据库或持久化模型。

## Module responsibilities
- `aats/services/strategy_engines/families/independent_family.py`
  - 在 close/reduce 场景补齐 leg `reference_price` 的最后一级回退，并显式要求调用方传入当前腿名义金额。
- `aats/services/operator/query_service.py`
  - 生成 leg trial-guard audit summary，并与 runtime/target 的 fallback 规则对齐。

## Inputs / outputs
- 输入：
  - independent book 当前腿持仓数量与名义金额
  - leg health payload（raw 与 guard-eligible 指标）
- 输出：
  - `StrategyLegIntent.reference_price`
  - operator leg trial-guard audit summary

## Data / consistency
- 不新增表、列、索引或约束。
- 仅消费现有 decision context / operator payload。

## Error handling
- expectancy 缺失时仅做价格回退，不改变既有 execution-policy gating。
- operator 视图在 guard-eligible 样本为空时回退 raw，不抛异常。

## Testing strategy
- unit:
  - full-close expectancy 缺失时仍生成 `reference_price`
  - operator leg trial-guard summary 在 guard-eligible window 为空时回退 raw 指标
- validation:
  - `ruff check`
  - `pytest tests/unit/`
  - 最窄 WSL2 integration test

## Compatibility / rollback
- 保持对外 public API 兼容；`build_independent_leg()` 仍是内部 helper，但现在要求显式传入 `current_leg_notional`。
- 失败时可直接回滚本次提交。

## Deployment / acceptance
- acceptance:
  - stale/failed full-close 不再因 expectancy 缺失而回退 `max_symbol_notional`
  - operator 面板与决策层对同一冷窗口样本的 trial-guard 判断一致
