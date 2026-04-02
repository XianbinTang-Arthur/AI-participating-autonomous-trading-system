---
name: system-no-order-debug
description: Diagnose why this automated trading system has signals but still does not place orders, create execution orders, or produce fills. Use when Codex needs to localize whether the stop happens in decision, allocator approval, family gating, execution bundle or plan generation, execution command enqueue, order creation, or venue reject and no-fill handling.
---

# Goal
Locate the earliest blocked layer in a no-order incident by following a fixed upstream-to-downstream path instead of starting from execution tables.

# Workflow
1. Confirm the issue is not caused by the wrong database, an inactive main loop, or missing persistence. Read the database connection source from `.env.derivatives.live` line 19 instead of guessing the DSN.
2. Investigate in this order and do not skip ahead:
   - `decision_audit_records`
   - `portfolio_allocation_decisions`
   - `strategy_sleeve_intents`
   - `strategy_execution_bundles`
   - `execution_commands`
   - `execution_orders`
   - `execution_fills`
3. Stop at the earliest missing upstream artifact. Do not continue downstream once the chain is already broken.
4. After obtaining a `decision_id`, search logs by `decision_id` and the matching layer-specific keywords instead of reasoning from logs alone.
5. Read [references/system-no-order-debug-playbook.md](references/system-no-order-debug-playbook.md) for SQL, field-level interpretation, common cause mapping, and log keywords.

# Output
Always return:
1. Blocked layer
2. Evidence
3. Most direct cause
4. Next thing to inspect

# Constraints
- Find the earliest missing artifact before naming a root cause.
- Do not default to blaming the execution layer.
- Prefer concrete table, field, and value evidence over abstract summaries.
