## Route A Regime-Slice Prefill SoW

### 背景

当前 `route-a-evidence-scaffold` 已能把以下内容自动写入 `proposal.md`：

- 元数据表
- `§4 Train / Test 分割`
- `§6.1 OOS`
- `§6.2 Cross-window`
- `§6.3 Cost-adjusted`
- 观察窗摘要

但 `scorecard.json` 已经具备的 `regime_slice.vol.low/high` 原始值，还没有落入 `proposal.md`。这会导致 future candidate 进入 evidence gate 时，`§6.4 Regime-slice` 仍然需要手工抄表。

### 目标

在**不改数值计算口径、不新增新切片逻辑、不触碰 live** 的前提下，让 `route-a-evidence-scaffold` 自动预填 `proposal.md` 的 `§6.4 Regime-slice` 段落，使用当前已有的：

- `scorecard.regime_slice.vol.low`
- `scorecard.regime_slice.vol.high`

### 业务边界

本任务只做：

1. 将 `regime_slice.vol.low/high` 的已有字段渲染到 `proposal.md`
2. 补相应最窄单测
3. 如有必要，补一份简短 SoW/说明文字

本任务明确不做：

- 不修改 `evidence_scorecard.py` 的 regime slice 计算逻辑
- 不新增 funding 方向 / 2x2 heatmap / realized vol 指标
- 不做任何 verdict / PASS / FAIL / Archive 文案
- 不更改 live path / config / deploy
- 不动 candidate discovery / research logic

### 预填范围

在 `proposal.md` 中新增：

#### `§6.4 Regime-slice (预填原始值)`

使用当前 scorecard 的单维切片：

| bucket | IR | fills | sample_n |
|---|---|---|---|
| low_vol | `regime_slice.vol.low.ir` | `...fills` | `...sample_n` |
| high_vol | `regime_slice.vol.high.ir` | `...fills` | `...sample_n` |

并补一段短说明：

- 当前自动预填只覆盖 **vol 单维切片**
- funding 方向 / 2x2 heatmap 仍需人工补或后续迭代
- 此处只列原始值，不给结论

### 输出要求

`proposal.md` 新增段落后必须满足：

1. UTF-8 中文
2. 不出现 `PASS` / `FAIL` / `Archive` / `Go` / `verdict`
3. 缺值场景继续用 `<TBD>`，不抛异常
4. 若 `regime_slice.vol` 缺失，也仍生成该段并带 `<TBD>`

### 建议测试

至少新增：

1. 富样本 scorecard 下，`proposal.md` 含 `§6.4 Regime-slice`
2. 出现 `low_vol` / `high_vol` 两行与对应数值
3. 缺失 `regime_slice` 子字段时，仍渲染且含 `<TBD>`
4. 不出现 verdict 文案

### 验证

最小必跑：

1. `.\.venv\Scripts\python.exe -m pytest tests/unit/test_route_a_evidence_scaffold.py tests/unit/test_cli_backtest.py -x -q --basetemp=./artifacts/_pytest_tmp`
2. `.\.venv\Scripts\python.exe -m ruff check aats/cli.py aats/data_platform/replay/backtest/route_a_evidence_scaffold.py tests/unit/test_route_a_evidence_scaffold.py`

完成后再由 PM 复跑全量 `ruff` 与 `tests/unit/`。
