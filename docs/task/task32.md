# Task 32: Stage 1 Implementation Checklist

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> **Historical document (2026-04 update)**: `ai_decision_maker_with_profile_control`
> 已从 canonical 运行模式枚举中移除；当前仅作为 legacy 兼容值保留在
> `AI_OPERATING_MODE_CANONICAL_MAP` 中（折叠为 `ai_decision_maker`）。

## 1. Goal

Stage 1 is the compatibility foundation for the AI decision-mainline refactor.

It does not rewrite the trading mainline yet.

It does:

- converge mode vocabulary
- introduce canonical mode normalization
- keep legacy mode compatibility
- land the first schema draft for `AIDecisionIntent` and `DecisionOutcome`

## 2. Stage 1 Deliverables

### 2.1 Mode Enum Convergence

Canonical values:

- `baseline_only`
- `ai_assisted`
- `ai_decision_maker`
- `ai_decision_maker_with_profile_control`

Legacy compatibility values:

- `ai_advisory`
- `ai_blended`
- `ai_primary`

Stage 1 rule:

- legacy values may still be parsed
- canonical values become the official target vocabulary
- downstream refactors should normalize to canonical mode before branching

### 2.2 Legacy-to-Canonical Mapping

| Legacy value | Canonical value |
|---|---|
| `baseline_only` | `baseline_only` |
| `ai_advisory` | `ai_assisted` |
| `ai_blended` | `ai_assisted` |
| `ai_primary` | `ai_decision_maker` |

### 2.3 Schema Drafts to Land in Code

New schema drafts:

- `AIDecisionIntent`
- `DecisionOutcome`

Optional supporting schema for later stages:

- `BaselineReference`

## 3. Files in Scope

Primary:

- `aats/schemas/decision.py`
- `aats/bootstrap/settings.py`
- `aats/services/ai_service/inference.py`

Initial test coverage:

- new Stage 1 normalization/schema tests
- existing AI inference and settings tests must continue passing

## 4. Implementation Order

### Step 1

In `aats/schemas/decision.py`:

- add canonical mode literal
- add legacy mode literal
- widen compatibility type so current code does not break
- add normalization helper:
  - `normalize_ai_operating_mode(...)`

### Step 2

In `aats/bootstrap/settings.py`:

- keep `ai_operating_mode` backward compatible
- add a canonical property:
  - `canonical_ai_operating_mode`

### Step 3

In `aats/services/ai_service/inference.py`:

- do not rewrite mainline behavior yet
- optionally expose canonical mode in a non-breaking way for future migration
- avoid changing existing decision-branch semantics in Stage 1

### Step 4

In `aats/schemas/decision.py`:

- add `AIDecisionIntent`
- add `DecisionOutcome`
- keep them unused by the mainline for now

### Step 5

Add tests:

- canonical normalization from legacy values
- canonical property on settings
- schema construction tests for `AIDecisionIntent` and `DecisionOutcome`

## 5. Non-Goals for Stage 1

Stage 1 does not:

- replace `ai_takeover_*`
- change `target_position` behavior
- change operator query semantics
- change frontend rendering
- remove legacy fields from payloads

## 6. Exit Criteria

Stage 1 is complete when:

- canonical mode vocabulary exists in code
- legacy values normalize correctly
- settings expose canonical mode consistently
- `AIDecisionIntent` and `DecisionOutcome` exist in code as stable schema drafts
- all new tests pass without breaking current mainline behavior
