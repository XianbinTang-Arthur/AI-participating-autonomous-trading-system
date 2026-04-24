# Task218 AI Full Enablement Governance Adaptation SOW

## Business Objectives And Boundaries
- Accept the operator decision that AATS should run with AI services fully enabled.
- Treat DeepSeek / OpenAI provider selection, `ai_decision_maker`, and `enabled_live` execution suggestions as allowed runtime behavior when explicitly configured and validated.
- Preserve the current trading microscope discipline: OKX + BTC-USDT-SWAP remains the live focus, `independent` remains the current live carrier, and execution truth / attribution truth remain required.
- This task adapts governance, automation state, and operator discipline. It does not add a strategy family, symbol, venue, release path, or promotion path.

## Module Responsibilities And Domain Model
- Automation state owns PM/Navigator task selection and must no longer classify approved AI enablement as a drift by itself.
- Project contribution discipline owns repository-wide guardrails and must distinguish system-approved AI trading from manual agent-triggered trades.
- Runtime AI provider code is owned by the existing DeepSeek provider / AI selector commit and is only validated here.

## Input / Output Interfaces
- Input: user instruction to fully enable system AI services, commit `b22660f` with DeepSeek provider and AI selector changes, and the existing PM/Navigator state.
- Output:
  - Automation prompt updated to the AI-enabled governance model.
  - `artifacts/automation/*` updated to recognize `b22660f` as approved AI enablement.
  - Repository discipline updated so AI mode changes are allowed only as bounded, reviewed, test-backed tasks.

## Database Schema / Tables / Indexes / Constraints
- No schema changes.
- No migration.
- No table/index/constraint changes.

## Transactions, Consistency, Concurrency
- No transaction or concurrency behavior changes.
- AI decisions must still flow through existing runtime state, risk, execution, and kill-switch controls.

## Authorization, Authentication, Data Security
- No secrets are read or printed.
- Provider keys remain environment/runtime configuration only.
- AI provider changes must not commit `.env.*.live`, `.env.wsl2`, API keys, passwords, or tokens.

## Error Handling And Idempotency
- Provider failure or missing API key must degrade through existing AI provider error handling.
- `AI_SELECTOR` is an operator-level override; invalid selector values must fail validation instead of silently choosing a provider.
- Re-running this governance update is idempotent: the result is the same AI-enabled rule set.

## State Transition And Lifecycle
- Old rule: AI must not lead trading.
- New rule: AI may lead configured decision / execution suggestion paths, but it may not bypass risk gates, execution guards, kill switch, truth-chain logging, symbol/venue/family scope, or release/promotion gates.
- Approved AI enablement is not drift; unreviewed AI expansion remains drift.

## Caching And Performance
- No caching changes.
- No performance-sensitive runtime path changes in this task.

## Logging, Monitoring, Auditing
- Current state must record:
  - AI provider target: `deepseek`
  - AI operating mode target: `ai_decision_maker`
  - AI execution suggestion target: `enabled_live`
  - Risk boundary: existing hard gates remain authoritative.

## Testing Strategy
- Validate DeepSeek provider / AI selector tests.
- Validate settings/profile tests affected by AI configuration.
- Validate task217 operator truth exposure test remains passing.
- Validate automation JSON remains machine-readable.

## Migration, Rollback, Compatibility
- Rollback by reverting this governance adaptation and, if needed, reverting `b22660f`.
- Existing OpenAI path remains supported through `AI_SELECTOR=OPENAI`.
- `AI_SELECTOR=DISABLED` remains the emergency provider-level disable path.

## Configuration And Environment Isolation
- Runtime provider selection is explicit through YAML or `AI_SELECTOR`.
- Secrets stay outside repository state.
- `AI_SELECTOR` has no `AATS_` prefix by design and is shell/compose level.

## Code Organization And Dependencies
- No new dependencies in this task.
- DeepSeek provider code remains in `aats/services/ai_service/`.

## Documentation And Operations Manual
- `CONTRIBUTING.md` is updated from “never enable AI decision maker” to “AI enablement requires bounded review, validation, and unchanged hard controls”.
- Automation prompt and state are updated to prevent future PM loops from blocking approved AI enablement as an old-rule violation.

## Deployment And Acceptance Criteria
- `artifacts/automation/current_state.json`, `task_registry.json`, and `navigator_review_state.json` parse successfully.
- The heartbeat automation prompt no longer forbids approved AI-led runtime behavior.
- DeepSeek / settings / env profile / task217 targeted tests pass.
- `git status` after commit is clean, or any residue is explicitly classified.
