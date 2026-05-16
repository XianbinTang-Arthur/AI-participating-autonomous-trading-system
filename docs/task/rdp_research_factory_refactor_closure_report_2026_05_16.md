# RDP Research Factory 重构验收 Closure Report

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

> 上级文档：[AATS RDP 研究工厂重构计划书](../rdp_research_factory_refactor_sow_2026_05_16.md)
> 执行 Playbook：[RDP Research Factory 重构实施 Playbook](rdp_research_factory_refactor_implementation_playbook_2026_05_16.md)
> Closure 时间：2026-05-16T11:42:13Z heartbeat run

## 1. Closure Summary

- Closure run id: `rf_closure_2026_05_16_114213`
- Reviewer: Codex automation heartbeat
- Review time UTC: `2026-05-16T11:42:13Z`
- Code version: working tree with untracked Research Factory files; no commit created in this run
- Overall status: `accepted_with_followups`
- Scope reviewed: Research Factory P0-P7 only
- Live runtime changed: `no`
- Production deployment performed: `no`
- Active parameter mutation performed: `no`

## 2. Task Card Checklist

| Task card | Required outcome | Evidence path | Status | Notes |
|-----------|------------------|---------------|--------|-------|
| RF-P0-01 | `ResearchStatus` lifecycle contract | `aats/data_platform/research_factory/status.py`; `tests/unit/data_platform/research_factory/test_status.py` | `[x]` | Status contract and terminal-state tests present. |
| RF-P0-02 | Base spec dataclasses and validation | `aats/data_platform/research_factory/specs.py`; `tests/unit/data_platform/research_factory/test_specs.py` | `[x]` | Dataset, segment, processor, label, experiment, and metrics specs covered. |
| RF-P0-03 | Artifact manifest writer and validator | `aats/data_platform/research_factory/artifacts.py`; `tests/unit/data_platform/research_factory/test_artifacts.py` | `[x]` | Atomic write, required fields, path traversal, status, and stable JSON covered. |
| RF-P0-04 | Research workflow spec | `aats/data_platform/research_factory/specs.py`; `tests/unit/data_platform/research_factory/test_specs.py` | `[x]` | Dataset stage and research-only workflow output checks covered. |
| RF-P1-01 | Time segment helpers and leakage guard | `aats/data_platform/research_factory/datasets/segments.py`; `tests/unit/data_platform/research_factory/test_segments.py` | `[x]` | Ratio, boundary, leakage, and replay overlap tests present. |
| RF-P1-02 | Gold bar dataset handler V1 | `aats/data_platform/research_factory/datasets/gold_bars.py`; `tests/unit/data_platform/research_factory/test_gold_bars_dataset.py` | `[x]` | Sorting, mismatch, duplicate timestamp, window filtering, and empty segment checks present. |
| RF-P1-03 | Dataset fingerprint and cache key | `aats/data_platform/research_factory/datasets/gold_bars.py`; `tests/unit/data_platform/research_factory/test_gold_bars_dataset.py` | `[x]` | Deterministic fingerprint and source watermark checks present. |
| RF-P2-01 | Safe factor DSL parser | `aats/data_platform/research_factory/features/expressions.py`; `tests/unit/data_platform/research_factory/test_factor_expressions.py` | `[x]` | Whitelist parser rejects import, attribute access, and future feature refs. |
| RF-P2-02 | Factor evaluator V1 | `aats/data_platform/research_factory/features/functions.py`; `tests/unit/data_platform/research_factory/test_factor_expressions.py` | `[x]` | Ref, return, rolling, null, zero division, and input immutability checks present. |
| RF-P2-03 | Baseline benchmark harness V1 | `aats/data_platform/research_factory/benchmarks/baseline.py`; `tests/unit/data_platform/research_factory/test_baseline_benchmark.py` | `[x]` | IC, Rank IC, net return proxy, and all-null rejection covered. |
| RF-P3-01 | Experiment recorder | `aats/data_platform/research_factory/experiments/recorder.py`; `tests/unit/data_platform/research_factory/test_experiment_recorder.py` | `[x]` | Running, terminal, failure, duplicate id, and relative output refs covered. |
| RF-P3-02 | Candidate artifact bridge and gate | `aats/data_platform/research_factory/metrics/gates.py`; `tests/unit/data_platform/research_factory/test_promotion_gates.py` | `[x]` | Cost, drawdown, critical metric, and no active parameter checks present. |
| RF-P4-01 | Metrics taxonomy | `aats/data_platform/research_factory/metrics/snapshots.py`; `tests/unit/data_platform/research_factory/test_metrics_snapshots.py` | `[x]` | Missing reason, merge conflict, and stable serialization checks present. |
| RF-P4-02 | Execution realism metric adapter | `aats/data_platform/research_factory/metrics/snapshots.py`; `tests/unit/data_platform/research_factory/test_metrics_snapshots.py` | `[x]` | Full fill, slippage, adjusted edge, missing file, and missing field checks present. |
| RF-P5-01 | Deterministic research allocation policy | `aats/data_platform/research_factory/allocation/policy.py`; `tests/unit/data_platform/research_factory/test_allocation_policy.py` | `[x]` | High MDD, missing metrics, epsilon floor, and reason trace covered. |
| RF-P6-01 | Sandbox proposal schema and guardrails | `aats/data_platform/research_factory/sandbox/proposal.py`; `aats/data_platform/research_factory/sandbox/guardrails.py`; `tests/unit/data_platform/research_factory/test_sandbox_guardrails.py` | `[x]` | Live write, env read, active parameter output, and research tmp pass checks present. |
| RF-P6-02 | Sandbox static scan V1 | `aats/data_platform/research_factory/sandbox/guardrails.py`; `tests/unit/data_platform/research_factory/test_sandbox_guardrails.py` | `[x]` | Environment access, network hint, forbidden path, and safe feature patch checks present. |
| RF-P7-01 | Baseline workflow config and closure template | `configs/research_factory/baseline_workflow.json`; `docs/task/rdp_research_factory_refactor_closure_template.md`; `tests/unit/data_platform/research_factory/test_baseline_workflow_config.py` | `[x]` | Baseline config is parseable and locked to research-only external triggering. |

## 3. SOW Acceptance Criteria

| Acceptance criterion | Evidence | Status | Notes |
|----------------------|----------|--------|-------|
| Same Research Factory inputs reproduce the same metrics | `DatasetSpec`, `dataset_fingerprint`, pure factor evaluator, baseline workflow config, and deterministic tests | `[x]` | No live data or secret reads are required for first-phase reproducibility. |
| Every candidate factor, model, or parameter has dataset, code, metrics, and artifact lineage | `ExperimentSpec`, `ExperimentRecorder`, `ArtifactManifest`, `CandidateArtifact` | `[x]` | First phase records candidates as JSON artifacts only. |
| Metrics include cost-adjusted returns, drawdown, execution feasibility, and stability coverage | `MetricsSnapshot`, `metrics/snapshots.py`, `baseline_workflow.json` | `[x]` | Required metrics include net return, drawdown, fillable ratio, partial fill ratio, and adjusted edge. |
| RD-Agent-style automation can only output candidates or recommendation drafts | `SandboxProposal`, `SandboxPolicy`, `scan_candidate_patch`, `baseline_workflow.json` | `[x]` | No code execution loop or RD-Agent dependency was introduced. |
| Governance gate is preserved; Research Factory cannot mutate live runtime state | `CandidateGateResult`, candidate-only workflow, sandbox write-root allowlist | `[x]` | No governance DB writes or active parameter apply path added. |
| No `.env*`, OKX key, token, password, or production credential was read or printed | Command history for this closure and sandbox guardrails | `[x]` | Closure read only docs/config/code/test files; no `.env*` content was opened. |
| No Qlib or RD-Agent production runtime dependency was introduced | Dependency files unchanged; Research Factory uses standard library only | `[x]` | `pyproject.toml`, requirement files, setup files unchanged in git status. |
| No live execution, OKX adapter, ledger, reconciliation, order lifecycle, or risk guard path was modified | Restricted-path `git status` check returned no changes | `[x]` | Research Factory work stayed under `aats/data_platform/research_factory`, `configs/research_factory`, `docs/task`, and focused tests. |
| Baseline workflow and closure template are present | `configs/research_factory/baseline_workflow.json`; `docs/task/rdp_research_factory_refactor_closure_template.md` | `[x]` | Added with parser test. |

## 4. Validation Results

| Command | Result | Notes |
|---------|--------|-------|
| `.venv\Scripts\python.exe -m ruff check aats\ --fix` | `All checks passed!` | Ran during closure. |
| `.venv\Scripts\python.exe -m ruff check tests\unit\data_platform\research_factory\ --fix` | `All checks passed!` | Ran during closure. |
| `.venv\Scripts\python.exe -m pytest tests\unit\data_platform\research_factory\ -q` | `123 passed, 1 warning in 0.76s` | Warning is existing `.pytest_cache` `WinError 183`. |
| `.venv\Scripts\python.exe -m pytest tests\unit\ -x -q` | First run failed during pytest `tmp_path` setup with `PermissionError` on default Windows temp; rerun with `TEMP/TMP=.pytest_workspace_tmp` passed: `3897 passed, 30 skipped, 1666 warnings, 82 subtests passed in 268.47s` | Failure mode was environment temp permission, not a test assertion. |
| Restricted forbidden-path status check | no output | Checked live execution, OKX, reconciliation, risk, API gateway, scripts, and dependency files. |
| `git diff --check` | passed | Ran after this report was written. |

## 5. Skipped Validations

| Validation | Reason | Risk accepted by |
|------------|--------|------------------|
| Narrow WSL2 integration test | Research Factory first phase added research-only files, configs, docs, and unit tests; no DB, API, scheduler, WSL2, or live runtime boundary changed. | Closure reviewer |
| Deployment | Explicitly forbidden by automation and unnecessary for research-only closure. | Closure reviewer |

## 6. Residual Risks And Followups

| Risk or followup | Severity | Owner | Due | Notes |
|------------------|----------|-------|-----|-------|
| Research Factory files remain untracked | Medium | Human reviewer | Before merge | Stage and commit intentionally if accepting this refactor. |
| Sandbox static scan is V1 hint-based | Low | RDP owner | Before executing generated code | Future code execution sandbox needs a dedicated SOW, stronger scanner, and isolated runner. |
| WSL2 integration not run | Low | RDP owner | If later wiring into DB/API/scheduler | Current scope is unit-tested research-only code. |
| Full unit suite default temp path failed before workspace-temp rerun | Low | Local environment owner | Optional | Workspace-temp rerun passed; default Windows temp ACL should be investigated separately if it recurs. |

## 7. Closure Decision

- Decision: `accepted_with_followups`
- Required followups before merge: review untracked file set, stage only intended Research Factory files, and commit intentionally.
- Required followups after merge: keep future RD-Agent/Qlib integration as research-only optional work under a new SOW.
- Reviewer notes: The implementation satisfies the Playbook task cards and SOW acceptance criteria for the AATS-native Research Factory first phase. No live trading runtime, OKX adapter, ledger, reconciliation, risk guard, deployment, secret, or active parameter apply path was touched during closure.
