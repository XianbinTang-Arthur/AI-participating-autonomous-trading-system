# Research Factory Observation Event Exporter SOW

Date: 2026-05-19

## Objective

Add a read-only normalization step that turns already-exported shadow/paper
event JSONL artifacts into canonical Research Factory observation event JSONL.
This lets the existing observation summary generator consume controlled
upstream evidence without adding another governance package, review, approval,
safety policy, execution layer, or runtime write path.

## Boundary

This exporter:

- reads only JSONL artifacts under `artifacts/research`
- writes only canonical observation event JSONL under `artifacts/research`
- does not read `.env`
- does not query live runtime tables
- does not write runtime config
- does not mutate active parameters
- does not call OKX
- does not create paper orders
- does not execute dry-runs

## Inputs

The CLI accepts:

```text
--source-events
--output-events
--recommendation-id
--candidate-id
--experiment-id
--mode shadow|paper
--source-kind shadow_decision|paper_intent|observation_event
--overwrite
```

Input records may be raw shadow/paper export rows with a nested `payload` or
already canonical observation events. Raw events must contain timestamp,
fillability, partial-fill, fee, slippage, funding, cost-adjusted edge,
drawdown, and metric drift evidence. Missing execution evidence fails closed.

## Outputs

The output is canonical `research_observation_event_v1` JSONL that can be passed
to `scripts/rdp_generate_research_observation_summary.py`.

## Acceptance

- invalid or incomplete metrics are rejected
- identity mismatches are rejected
- input and output paths must stay under `artifacts/research`
- existing output files require explicit `--overwrite`
- CLI success and failure are machine-readable JSON
- runtime mutation, active parameter writes, runtime config writes, OKX writes,
  and dry-run execution remain disallowed
