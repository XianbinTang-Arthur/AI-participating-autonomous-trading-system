# Task 227 - Decision Drawer Truth Semantics Cleanup

## Business Objectives And Boundaries

The strategy decision detail drawer must present decision facts without implying orders, AI control, or overlay activity that did not occur. This task only changes UI rendering and tests. It does not change trading logic, risk evaluation, execution planning, API contracts, database writes, or runtime configuration.

## Module Responsibilities And Domain Model

- `aats/api/static/modules/detail-drawers.js` owns the decision detail drawer sections and wording.
- `aats/api/static/modules/trade-display.js` owns common trade row rendering shared by decision/order/fill surfaces.
- `aats/api/static/modules/terms.js` owns enum/reason localization and compact state summaries.
- Decision domain facts remain:
  - final target/action come from `decision_outcome` and `position_target`;
  - gate pass means not blocked, not necessarily submit;
  - AI economic actionability means AI adoption gate/economic discipline, not raw service availability;
  - overlay parent signal fields must only produce a sentence when concrete parent fields exist.

## Input/Output Interfaces

Input is the existing decision detail payload returned by `/decision/{decision_id}`. Output remains the same `buildDecisionDrawer(detail)` object with `eyebrow`, `title`, `summary`, and `body` HTML. No API schema changes.

## Database Schema / Tables / Indexes / Constraints

No database changes.

## Transactions, Consistency, Concurrency

No transactional behavior changes. Rendering remains deterministic from one payload.

## Authorization, Authentication, Data Security

No auth changes. The UI may display non-secret model metadata such as model name, prompt version, output validity, and latency. It must not expose credentials or provider secrets.

## Error Handling And Idempotency

Missing/null fields must render as absent or "not available" instead of producing fabricated explanatory sentences. Re-rendering the same payload remains idempotent.

## State Transition And Lifecycle

No trading lifecycle state transition changes. The drawer should distinguish:

- no-order hold state;
- gate not blocked;
- AI evaluated but not adopted;
- independent book inactive because score is below entry threshold.

## Caching And Performance

No fetch/cache changes. String rendering remains local and lightweight.

## Logging, Monitoring, Auditing

No logging changes. The drawer will expose audit-relevant model metadata already present in payloads.

## Testing Strategy

Add or update Node-render integration coverage in `tests/integration/test_dashboard_ui.py` for:

- flat hold decisions hide misleading order sizing;
- pass gates display as "not blocked";
- AI fallback/adoption wording no longer says direct takeover;
- model/latency/output validity are visible when provided;
- empty overlay parent fields do not generate placeholder sentences;
- below-entry independent reason text says no opening, not closing an existing book;
- absent AI execution suggestion does not render an empty execution suggestion section.

## Migration, Rollback, Compatibility

Rollback is reverting the static UI and test changes. Existing payloads remain compatible. Existing non-flat sizing summaries should continue to render.

## Configuration And Environment Isolation

No environment variable changes.

## Code Organization And Dependencies

No new dependencies. Keep changes inside existing frontend helper modules and tests.

## Documentation And Operations Manual

This SOW is the task record. No operator procedure changes.

## Deployment And Acceptance Criteria

Acceptance criteria:

- Target zero / hold decision does not show "下单规模分解".
- Policy/risk pass copy says "未阻断".
- AI fallback copy says AI was evaluated but not adopted due to gate/economic discipline.
- Drawer displays provider model metadata and latency when available.
- Overlay parent signal summary stays empty when all parent signal fields are null/undefined.
- Independent below-entry reason says entry threshold is not met.
- Tests pass for the focused drawer rendering behavior.
