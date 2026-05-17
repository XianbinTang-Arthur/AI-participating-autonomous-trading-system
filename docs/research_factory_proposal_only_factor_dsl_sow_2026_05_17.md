# Research Factory Proposal-only Factor DSL Integration SOW

## Business Objectives and Boundaries

Add a proposal-only research input path where an automation or human can submit only a hypothesis, a safe Factor DSL expression, and rationale. The proposal must enter the existing research-only chain:

```text
FactorDSLProposal -> NoveltyGate -> RealDataRunner -> EvidenceBundle -> CandidateArtifact -> ResearchRecommendation
```

The work must not add generated Python execution, live runtime mutation, active parameter apply, OKX write access, production deployment behavior, or Qlib/RD-Agent production dependencies.

## Module Responsibilities and Domain Model

- `proposals.py`: validate and normalize proposal-only payloads.
- `real_data.py`: persist the proposal as an experiment artifact and require the executed factor expression to match the proposal.
- `rdp_run_research_factory_experiment.py`: accept a proposal JSON file as an input source.
- Existing novelty, evidence, candidate, recommendation, and registry modules remain the authoritative downstream gates.

## Input/Output Interfaces

Input proposal JSON is intentionally narrow:

```json
{
  "hypothesis": "...",
  "factor_expression": "Return(close, 1)",
  "rationale": "..."
}
```

Output artifact:

```text
factor_proposal.json
```

The proposal ref is recorded in the experiment manifest, candidate payload, recommendation evidence refs, and CLI result.

## Database Schema / Tables / Indexes / Constraints

No database schema changes. Real-data loading remains read-only through the existing Gold replay adapter.

## Transactions, Consistency, Concurrency

Proposal artifact writes use the existing experiment recorder atomic JSON path. A proposal/factor mismatch fails before candidate generation.

## Authorization, Authentication, Data Security

Proposal files must be JSON research artifacts under `artifacts/research`. Proposal text rejects runtime/apply/live/order/code-generation terms and does not permit arbitrary code or file patch payloads.

## Error Handling and Idempotency

Invalid proposals fail closed with a clear validation error. Runner overwrite semantics remain unchanged.

## State Transition and Lifecycle

Proposal validation occurs before feature execution. If the experiment starts and a downstream gate fails, the existing failure artifact and registry memory behavior applies.

## Caching and Performance

No new caching. Proposal parsing is local and negligible.

## Logging, Monitoring, Auditing

The proposal becomes an auditable artifact attached to the experiment manifest and recommendation evidence package.

## Testing Strategy

Add unit tests for proposal schema restrictions, DSL validation, forbidden runtime/code terms, and real-data runner proposal artifact propagation.

## Migration, Rollback, Compatibility

The CLI remains backward compatible with `--factor-expression`; `--factor-proposal` is additive and can provide the expression when `--factor-expression` is omitted.

## Configuration and Environment Isolation

No new environment variables. The CLI still reads only the configured DB URL environment variable and does not read `.env` files.

## Code Organization and Dependencies

Use only existing standard library and Research Factory modules. No new third-party dependencies.

## Documentation and Operations Manual

This SOW documents the proposal-only contract. Operators should treat proposal artifacts as research inputs, not promotion or apply instructions.

## Deployment and Acceptance Criteria

Acceptance requires lint, unit tests, and a targeted WSL2 integration sanity check. The change is accepted when proposal-only runs produce `factor_proposal.json` and downstream recommendation evidence without any live runtime write path.
