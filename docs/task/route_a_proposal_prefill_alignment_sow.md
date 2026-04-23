## Route A Proposal Prefill Alignment SoW

### 背景

当前 `route-a-evidence-scaffold` 已能复制：

- `scorecard.json`
- `observation_window_summary.json`
- `manifest.json`

并生成最小 `proposal.md` 骨架，但 `proposal.md` 仍只包含 metadata + artifact 引用。  
Route A phase 0 模板中，已有一批字段其实已经能从现有 `evidence_scorecard` 与 observation-window summary 机械导出，不应继续手工抄表。

### 目标

在**不触碰 live path / config / deploy / verdict** 的前提下，让 `route-a-evidence-scaffold` 预填 `proposal.md` 中当前已具备数据来源的关键段落，降低未来真 candidate 进入 evidence gate 的手工成本。

### 严格边界

本任务**只允许**：

1. 扩展 `aats/data_platform/replay/backtest/route_a_evidence_scaffold.py`
2. 如有必要，扩展 `aats/cli.py` 的 `route-a-evidence-scaffold` 输出
3. 新增/更新最窄单测

本任务**明确不做**：

- 不生成 `PASS/FAIL/Go/Archive` 等 verdict 文案
- 不新增任何 candidate discovery 逻辑
- 不修改 `evidence_scorecard` 数值计算口径
- 不改 live runtime / WSL2 deploy / configs
- 不补齐模板中当前无数据来源的章节（如 §2 特征统计、§3 模型定义、§7 加分项、§8 红旗自查）

### 允许预填的段落

#### 1. 元数据 (提案头)

从以下来源预填：

- `proposal_id`, `feature`, `horizon`, `proposer`: CLI 输入 / manifest
- `symbol`, `timeframe`, `dataset_version`, `order_type`: `scorecard.meta`
- `提案日期`: scaffold `generated_at`
- `Scope: time range`: `scorecard.meta.start_ts/end_ts` + `scorecard.oos.split_ts`

#### 2. §4 Train / Test 分割

从 `scorecard.oos` 预填：

- `train_start/train_end`
- `test_start/test_end`
- `split_method`
- `split_ts`

只写事实，不写“边界理由”判断。

#### 3. §6.1 OOS

从 `scorecard.oos.train/test` 预填：

- `IR (annualized)`
- `Sharpe`
- `Hit rate`
- `Max drawdown`
- `Sample N`

#### 4. §6.2 Cross-window

从 `scorecard.cross_window[*]` 预填每个 slice：

- `start/end`
- `ir_annualized`
- `hit_rate`
- `max_drawdown_bps`
- `sample_n`

slice label 允许按 `S1/S2/S3...` 自动生成。

#### 5. §6.3 Cost-adjusted

从 `scorecard.cost_adjusted.train/test` 预填：

- `realized_edge_bps`
- `fee_bps`
- `slip_bps`
- `exec_buffer_bps`
- `net_edge_bps`

并额外在小节末尾写明 sensitivity 原始数值：

- `cost_adjusted.sensitivity.train`
- `cost_adjusted.sensitivity.test`

只列值，不做“是否仍 > 0”的结论。

#### 6. 观察窗引用

在 `proposal.md` 中新增一个简短小节，引用 `observation_window_summary.json` 的：

- `overall`
- `window_start`
- `window_target`
- `warn_count`
- `fail_count`

目的仅是把 candidate evidence 与观察窗真相层绑在一起。

### 输出要求

`proposal.md` 必须满足：

1. 仍然是 UTF-8 中文文档
2. 仍然保留“待填内容”提示，明确说明哪些章节尚需人工填写
3. 不出现 verdict / archive / pass / fail 等 gate 裁决词
4. 所有自动填入的数值都可追溯到 `scorecard.json` / `observation_window_summary.json`

### 建议测试

至少覆盖：

1. scaffold 生成的 `proposal.md` 包含：
   - 元数据表
   - §4 train/test 边界
   - §6.1 OOS 表
   - §6.2 cross-window 表
   - §6.3 cost-adjusted 表
   - observation-window 摘要
2. `proposal.md` 不包含：
   - `PASS`
   - `FAIL`
   - `Archive`
   - `Go`
   - `verdict`
3. BOM JSON 输入仍能成功生成 bundle

### 验证

最小必跑：

1. `.\.venv\Scripts\python.exe -m pytest tests/unit/test_route_a_evidence_scaffold.py tests/unit/test_cli_backtest.py -x -q --basetemp=./artifacts/_pytest_tmp`
2. `.\.venv\Scripts\python.exe -m ruff check aats/cli.py aats/data_platform/replay/backtest/route_a_evidence_scaffold.py tests/unit/test_route_a_evidence_scaffold.py`

如实现触及其他共享 helper，再补跑最窄受影响单测。
