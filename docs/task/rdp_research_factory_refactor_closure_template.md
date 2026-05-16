# RDP Research Factory 重构验收 Closure Report

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

> 模板状态：closure review 模板
> 上级文档：[AATS RDP 研究工厂重构计划书](../rdp_research_factory_refactor_sow_2026_05_16.md)
> 执行 Playbook：[RDP Research Factory 重构实施 Playbook](rdp_research_factory_refactor_implementation_playbook_2026_05_16.md)

## 1. Closure Summary

- Closure run id:
- Reviewer:
- Review time UTC:
- Code version:
- Overall status: `pending_review` / `accepted` / `accepted_with_followups` / `rejected`
- Scope reviewed: Research Factory P0-P7 only
- Live runtime changed: `no`
- Production deployment performed: `no`
- Active parameter mutation performed: `no`

## 2. Task Card Checklist

| Task card | Required outcome | Evidence path | Status | Notes |
|-----------|------------------|---------------|--------|-------|
| RF-P0-01 | `ResearchStatus` lifecycle contract | `aats/data_platform/research_factory/status.py`; `tests/unit/data_platform/research_factory/test_status.py` | `[ ]` | |
| RF-P0-02 | Base spec dataclasses and validation | `aats/data_platform/research_factory/specs.py`; `tests/unit/data_platform/research_factory/test_specs.py` | `[ ]` | |
| RF-P0-03 | Artifact manifest writer and validator | `aats/data_platform/research_factory/artifacts.py`; `tests/unit/data_platform/research_factory/test_artifacts.py` | `[ ]` | |
| RF-P0-04 | Research workflow spec | `aats/data_platform/research_factory/specs.py`; `tests/unit/data_platform/research_factory/test_specs.py` | `[ ]` | |
| RF-P1-01 | Time segment helpers and leakage guard | `aats/data_platform/research_factory/datasets/segments.py`; `tests/unit/data_platform/research_factory/test_segments.py` | `[ ]` | |
| RF-P1-02 | Gold bar dataset handler V1 | `aats/data_platform/research_factory/datasets/gold_bars.py`; `tests/unit/data_platform/research_factory/test_gold_bars_dataset.py` | `[ ]` | |
| RF-P1-03 | Dataset fingerprint and cache key | `aats/data_platform/research_factory/datasets/gold_bars.py`; `tests/unit/data_platform/research_factory/test_gold_bars_dataset.py` | `[ ]` | |
| RF-P2-01 | Safe factor DSL parser | `aats/data_platform/research_factory/features/expressions.py`; `tests/unit/data_platform/research_factory/test_factor_expressions.py` | `[ ]` | |
| RF-P2-02 | Factor evaluator V1 | `aats/data_platform/research_factory/features/functions.py`; `tests/unit/data_platform/research_factory/test_factor_expressions.py` | `[ ]` | |
| RF-P2-03 | Baseline benchmark harness V1 | `aats/data_platform/research_factory/benchmarks/baseline.py`; `tests/unit/data_platform/research_factory/test_baseline_benchmark.py` | `[ ]` | |
| RF-P3-01 | Experiment recorder | `aats/data_platform/research_factory/experiments/recorder.py`; `tests/unit/data_platform/research_factory/test_experiment_recorder.py` | `[ ]` | |
| RF-P3-02 | Candidate artifact bridge and gate | `aats/data_platform/research_factory/metrics/gates.py`; `tests/unit/data_platform/research_factory/test_promotion_gates.py` | `[ ]` | |
| RF-P4-01 | Metrics taxonomy | `aats/data_platform/research_factory/metrics/snapshots.py`; `tests/unit/data_platform/research_factory/test_metrics_snapshots.py` | `[ ]` | |
| RF-P4-02 | Execution realism metric adapter | `aats/data_platform/research_factory/metrics/snapshots.py`; `tests/unit/data_platform/research_factory/test_metrics_snapshots.py` | `[ ]` | |
| RF-P5-01 | Deterministic research allocation policy | `aats/data_platform/research_factory/allocation/policy.py`; `tests/unit/data_platform/research_factory/test_allocation_policy.py` | `[ ]` | |
| RF-P6-01 | Sandbox proposal schema and guardrails | `aats/data_platform/research_factory/sandbox/proposal.py`; `aats/data_platform/research_factory/sandbox/guardrails.py`; `tests/unit/data_platform/research_factory/test_sandbox_guardrails.py` | `[ ]` | |
| RF-P6-02 | Sandbox static scan V1 | `aats/data_platform/research_factory/sandbox/guardrails.py`; `tests/unit/data_platform/research_factory/test_sandbox_guardrails.py` | `[ ]` | |
| RF-P7-01 | Baseline workflow config and closure template | `configs/research_factory/baseline_workflow.json`; this template | `[ ]` | |

## 3. SOW Acceptance Criteria

| Acceptance criterion | Evidence | Status | Notes |
|----------------------|----------|--------|-------|
| Same Research Factory inputs reproduce the same metrics | | `[ ]` | |
| Every candidate factor, model, or parameter has dataset, code, metrics, and artifact lineage | | `[ ]` | |
| Metrics include cost-adjusted returns, drawdown, execution feasibility, and stability coverage | | `[ ]` | |
| RD-Agent-style automation can only output candidates or recommendation drafts | | `[ ]` | |
| Governance gate is preserved; Research Factory cannot mutate live runtime state | | `[ ]` | |
| No `.env*`, OKX key, token, password, or production credential was read or printed | | `[ ]` | |
| No Qlib or RD-Agent production runtime dependency was introduced | | `[ ]` | |
| No live execution, OKX adapter, ledger, reconciliation, order lifecycle, or risk guard path was modified | | `[ ]` | |
| Baseline workflow and closure template are present | | `[ ]` | |

## 4. Validation Results

| Command | Result | Notes |
|---------|--------|-------|
| `.venv\Scripts\python.exe -m ruff check aats\ --fix` | | |
| `.venv\Scripts\python.exe -m pytest tests\unit\data_platform\research_factory\ -q` | | |
| `.venv\Scripts\python.exe -m pytest tests\unit\ -x -q` | | |
| Narrow WSL2 integration test, if affected | | |
| `git diff --check` | | |

## 5. Skipped Validations

| Validation | Reason | Risk accepted by |
|------------|--------|------------------|
| | | |

## 6. Residual Risks And Followups

| Risk or followup | Severity | Owner | Due | Notes |
|------------------|----------|-------|-----|-------|
| | | | | |

## 7. Closure Decision

- Decision:
- Required followups before merge:
- Required followups after merge:
- Reviewer notes:
