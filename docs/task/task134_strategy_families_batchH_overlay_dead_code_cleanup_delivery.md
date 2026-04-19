# Task 134 - Strategy Families Batch H Delivery

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Scope

Batch H 的目标是继续清理 `target_position.py` 中遗留的 legacy overlay 评估入口，并验证 `protective / opportunistic / independent` 在主链切流后不再依赖 `directional` 内嵌 hedge path 才能运行。

本批次要求：

- 删除 `target_position.py` 中 legacy overlay helper 和调用残留
- 让 `target_position.py` 只负责 directional 主腿目标与 hedge 模式下的主腿执行腿
- 由 family engines 独占 `protective / opportunistic / independent` 的评估入口
- 补 2 条真实 mainline integration：
  - `opportunistic`
  - `independent`

## What Changed

### 1. `target_position.py` 不再评估 overlay

`TargetPositionEngine._build()` 不再调用任何 legacy overlay 评估入口：

- 不再进入 `_hedge_mode_strategy_legs()`
- 不再在 `target_position.py` 里生成 legacy overlay decision
- 不再在 `target_position.py` 里生成 protective / opportunistic / independent overlay legs

现在 `target_position.py` 在 `hedge` 运行域下只做一件事：

- 把 directional 净目标拆成 hedge 模式下的 primary long/short execution legs

这一步保留了 directional 在 `hedge` 模式下的执行能力，同时去掉了对 overlay family 评估的所有内嵌依赖。

### 2. overlay family 评估入口完全收敛到 family engines

`protective / opportunistic / independent` 的业务评估入口现在只在 family modules 中存在：

- `aats/services/strategy_engines/families/protective_family.py`
- `aats/services/strategy_engines/families/opportunistic_family.py`
- `aats/services/strategy_engines/families/independent_family.py`

`target_position.py` 不再承担 overlay candidate / overlay decision 的构造职责。

### 3. mainline integration 证明 family cutover 后仍能运行

新增真实主链集成验证：

- `test_opportunistic_family_cutover_runs_without_legacy_target_overlay_path`
- `test_independent_family_cutover_runs_without_legacy_target_overlay_path`

验证点：

- raw base target 不再携带 legacy overlay summary
- raw base target 只保留 directional primary legs
- 最终 applied target 仍能由 family candidate 接管，生成正确的 family legs / overlay summary / execution metadata

## Behavioral Outcome

Batch H 完成后，系统分工变为：

- `target_position.py`
  - directional target
  - hedge 模式下 directional primary legs
- family engines
  - protective evaluation
  - opportunistic evaluation
  - independent evaluation
- coordinator / allocator / apply
  - family 选择
  - overlay summary 回填
  - final execution leg cutover

这意味着 `protective / opportunistic / independent` 现在已经不再依赖 `directional` 内嵌 overlay path 才能运行。

## Compatibility

- 顶层 `PositionTarget.hedge_overlay_decision` 字段仍保留
- runtime / audit / UI 仍可读取 overlay summary
- summary 的来源从 `target_position.py` 内嵌评估，切换为 `coordinator` 基于 selected/configured family candidate 的回填

## Validation

已覆盖：

- target position unit regression
- real mainline integration for family cutover
- full repo lint

`verify.sh` 仍无法运行，原因是当前环境的 `bash.exe` 不可访问，不是本批次代码逻辑失败。
