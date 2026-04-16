# P0 Profitability Follow-up Bugfix SoW

## Business objectives and boundaries
- 修复 independent open gate 中 anomaly cost fuse 被静默切成 lifecycle 成本语义的问题。
- 修复 lifecycle 成本估计在 exit estimate 异常为负时可能回退成双倍 entry 成本的问题。
- 修复 lifecycle attribution 对“仅候选且强关联缺失”的完整性分类，并补齐 candidate diagnostics 字段。
- 修复 lifecycle drawer 中残留的英文用户文案，统一为 UTF-8 中文。
- 不改 public API 路径，不改主视图默认账单口径，不改 live 参数。

## Module responsibilities and domain model
- `aats/services/strategy_engines/independent/gates.py`
  负责 independent open eligibility 与 anomaly cost fuse；safe-edge 继续看 lifecycle net edge，cost fuse 回到单边成本语义。
- `aats/services/strategy_engines/families/independent_family.py`
  负责 independent lifecycle expectancy 估计；exit cost fallback 不能重复叠加 entry 成本。
- `aats/services/operator/lifecycle_attribution.py`
  负责 lifecycle detail 归因；candidate diagnostics 仅作弱证据展示，但字段集需足以支持诊断。
- `aats/api/static/modules/lifecycle-drawer.js`
  负责 lifecycle detail 渲染；用户可见文案统一中文，并准确表达“仅候选证据 + 强关联缺失”。

## Input/output interfaces
- `evaluate_open_eligibility()`：
  - safe edge 比较继续优先使用 `expected_lifecycle_net_edge_bps`
  - anomaly cost fuse 继续比较单边 `expected_cost_bps`
- `anomaly_cost_fuse_threshold_bps()`：
  - headroom 继续使用单边 `expected_net_edge_bps`
- `/reports/position-lifecycle-attribution/{lifecycle_id}`：
  - `candidate_decisions` 字段集补齐为诊断级 payload
  - `trace_completeness` 对“无强匹配但有候选且缺失强关联证据”返回 `candidate_only`

## Database schema / tables / indexes / constraints
- 无 schema 变更。

## Transactions, consistency, concurrency
- 仅修改纯计算和诊断渲染逻辑，不新增写路径。

## Authorization, authentication, data security
- 不新增权限面。
- 不读取、不暴露凭证。

## Error handling and idempotency
- lifecycle attribution 在强关联审计缺失时继续 fail-soft：
  - 不抛出异常
  - 通过 `missing_linked_reference_count` 和 `trace_completeness` 暴露证据边界

## State transition and lifecycle
- open gate：
  - lifecycle net edge 决定“是否值得开”
  - 单边 expected cost 决定“成本是否异常”
- lifecycle candidate diagnostics：
  - 弱关联候选不进入主 trace
  - 但需携带足够的 expectancy / health / fee-drag 上下文

## Caching and performance
- 无新增缓存层。
- lifecycle candidate payload 复用现有归因计算，不增加额外查询。

## Logging, monitoring, auditing
- 无新增日志事件。

## Testing strategy
- 单测覆盖：
  - anomaly fuse 继续使用单边成本/单边净边际
  - lifecycle cost fallback 不重复叠加 entry 成本
  - `candidate_only + missing_linked_reference_count > 0` 的 completeness 语义
  - candidate diagnostics 字段补齐
- 集成测试覆盖：
  - lifecycle drawer 中文文案与候选诊断展示

## Migration, rollback, compatibility
- 全部为向后兼容的实现修正与字段补充。
- 回滚仅需代码回退。

## Configuration and environment isolation
- 不新增配置项。

## Code organization and dependencies
- 保持修改局限在 independent gate / lifecycle attribution / lifecycle drawer / 相关测试。

## Documentation and operations manual
- 本 SoW 作为本轮 follow-up bugfix 的边界说明。

## Deployment and acceptance criteria
- open gate 的 anomaly cost fuse 不再把单边预算静默改成 lifecycle 预算。
- lifecycle cost 在 exit estimate 退化时不再意外双算 entry 成本。
- lifecycle detail 能区分：
  - 完整强关联 trace
  - 仅候选证据
  - 强关联证据缺失
- lifecycle drawer 无残留英文用户文案。
