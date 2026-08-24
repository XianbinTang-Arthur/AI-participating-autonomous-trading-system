# AI Prompt 对 Baseline 新 reason_codes 的回归调研（2026-04-19）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## TL;DR

- **风险级别：A（有实质风险）** — AI 在 shadow 模式下已活跃调用 OpenAI，新 `alpha_basis_contrarian_*` code 在过去 30 分钟已触发 33 次进入真实 prompt。
- **已实施修复**：向 `prompt_builder.build()` 的 JSON payload 注入条件式 `baseline_reason_code_glossary` 字段（仅在对应 code 出现时填充），覆盖 P1.4–P2.7 新增的 8 个衍生品专属 alpha code；`ai_prompt_version` 同步从 `0.2.0` 升到 `0.3.0`。
- **测试**：新增 5 个 prompt_builder 单测 + 现有 2390 个单测全部通过。
- **未 deploy，未 push**。

## 1. Prompt 结构摘要

```
OpenAI /v1/responses
  model = gpt-4.1-mini   (configs/strategy_profiles/derivatives_live.yaml:24)
  temperature = 0
  text.format = json_schema (strict)
  input:
    - role: system
      content: "You are a deterministic market analysis model. Return only valid JSON that matches the provided schema."
    - role: user
      content: <prompt_builder.build() 输出的单行 ASCII-escaped JSON>

prompt_builder.build() payload（prompt 旧版 / 0.2.0）:
  {
    "task": "ai_primary_market_assessment",
    "operating_mode": "...",
    "instructions": { "goal": ..., "requirements": [...] },
    "decision_brief": {           # AIDecisionBrief.model_dump(mode="json")
      ...
      "baseline_reason_codes": ["alpha_basis_contrarian_long", ...],  # <--- 新 code 流入位置
      ...
    },
    "response_contract": { ..., "override_reason_codes": ["string"] }   # AI 可能回显 code
  }
```

## 2. 新 code 清单

| Code | 含义 | 引入版本 |
|---|---|---|
| `alpha_basis_contrarian_long` | basis (last_trade − mark) 负向极值 → 反转多 | P1.4 |
| `alpha_basis_contrarian_short` | basis 正向极值 → 反转空 | P1.4 |
| `alpha_funding_long_bias` | funding 偏多拥挤 → 反转偏空 | P1.5 |
| `alpha_funding_short_bias` | funding 偏空拥挤 → 反转偏多 | P1.5 |
| `alpha_oi_long_confirming` | OI 扩张 + 价涨 → 顺势多 | P1.6 |
| `alpha_oi_short_confirming` | OI 扩张 + 价跌 → 顺势空 | P1.6 |
| `alpha_ls_contrarian_long` | 大户多空比偏低 → 反转多 | P2.7 |
| `alpha_ls_contrarian_short` | 大户多空比偏高 → 反转空 | P2.7 |

Source: [aats/services/decision_engine/baseline.py:343](../../aats/services/decision_engine/baseline.py:343)-373

### AI 误解点

- `ls_contrarian_long` 可能被预训练语料里最接近的概念（least-squares / long-short equity）混淆。
- `funding_long_bias` 语义颠倒：名字说"多头偏向"，信号含义实际是"反转看空"。命名反直觉，最高风险。
- `basis_contrarian_*` 名字勉强可读但 `basis` 在加密衍生品 vs 传统期货 vs 会计语境里有不同默认含义。
- `oi_*_confirming` 相对清晰但 AI 可能把 `oi` 当成普通英文词。

## 3. Shadow 数据采样（2026-04-19 14:30 CST，部署约 30 分钟）

### 3.1 Baseline reason_codes 实际触发频率（最近 6 小时）

来源：`event_store` 表，`topic='strategy.baseline_assessment'`，共 893 条事件。

| Code | 频次 | 占比 |
|---|---|---|
| `baseline_multi_factor_alpha` | 893 | 100% |
| `alpha_regime_support` | 471 | 52.7% |
| `alpha_momentum_support` | 311 | 34.8% |
| `alpha_trend_support` | 247 | 27.7% |
| `alpha_multi_timeframe_support` | 9 | 1.0% |
| **`alpha_basis_contrarian_long`** | **21** | **2.4%** |
| **`alpha_basis_contrarian_short`** | **12** | **1.3%** |
| `alpha_funding_*` | 0 | 0.0% |
| `alpha_oi_*` | 0 | 0.0% |
| `alpha_ls_*` | 0 | 0.0% |

聚合校验（所有 8 个新 code 触发数）：33 条 baseline_assessment 在 14:03–14:30 触发了新 code（100% 是 basis 类）。

### 3.2 AI 是否已消费这些 prompt？

```
docker logs aats-decision | grep api.openai.com
  2026-04-19T05:54:19Z POST .../v1/responses 200
  2026-04-19T05:55:19Z POST .../v1/responses 200
  ... (大约每 30–60 秒一次，连续)
```

- `ai_operating_mode = baseline_only` → live 决策走 baseline
- `ai_shadow_mode_enabled = true` → **AI shadow 路径仍在调用 OpenAI**
- 结论：**新 code 已经进入真实 prompt**（约 3.7% 的 shadow 调用携带 basis code）

### 3.3 Shadow evaluation 持久化

- `strategy.ai_*` topics 在 event_store 表 0 条。
- [aats/services/ai_service/evaluator.py:11](../../aats/services/ai_service/evaluator.py:11) 的 `AIEvaluationTracker` 把 shadow 结果保存在 `dict`/`list` in-memory 内，只维护最近 500/200 条。
- 这意味着**无法用 SQL 对齐调研**历史 shadow 输出质量。但不影响风险判断：OpenAI 已调用 = prompt 已污染。

## 4. 风险评估

| 面向 | 当前影响 | 未来影响 |
|---|---|---|
| Live 决策质量 | 无（baseline_only）| 一旦切 `ai_assisted`，AI 误读可直接进 `override_reason_codes` → 下游 `target_position_engine` 审计日志和 operator UI 都会回显脏短码 |
| Shadow vs baseline 比较公平性 | **有** — AI 在看不懂的 code 下做决策，`ai_shadow_underperformed_baseline` 会偏向 baseline，推迟 AI 放行时点 | 同上 |
| Audit / operator UI | 无 | AI 回显看不懂的短码 → operator 难以归因失败 |
| Decision_brief schema | 无 | 保留 `baseline_reason_codes: list[str]`，无破坏 |

结论：**现在（shadow）有实质风险，未来（ai_assisted）风险更大**。补 glossary 是低成本高收益，符合 `质量优先于速度` 的原则。

## 5. 修复方案

最小侵入：在 `prompt_builder.build()` 的 JSON payload 里加一个**条件式** top-level 字段 `baseline_reason_code_glossary`——仅在 `brief.baseline_reason_codes` 出现了已知新 code 时填充 entry。

### 5.1 决策要点

- **条件注入 vs 总是注入**：选条件。temp=0 下总是注入也不会扭曲输出，但 90% 场景下 glossary 为空，条件式更省 token + 可观测性更好（operator 看到 glossary 非空立即知道新 code 触发）。
- **字段位置**：与 `decision_brief` 平级，而不是嵌进 brief 内部。原因：schema 已定义 `baseline_reason_codes: list[str]`，加 glossary 字段到 brief 会污染 schema；放 payload 顶层只影响 prompt，不影响 event_store 里 brief 的持久化形态。
- **Instructions 新增规则**："Use glossary only to interpret; do not echo / do not mention in output"—防止 AI 把 glossary 内容回填到 `rationale_summary`，保持输出干净。
- **版本号**：`ai_prompt_version` 从 `0.2.0` 升到 `0.3.0`，切开前后 shadow evaluation 历史窗口。

### 5.2 Diff

```diff
# aats/services/ai_service/prompt_builder.py
+ REASON_CODE_GLOSSARY = {
+     "alpha_basis_contrarian_long": "Basis (last_trade − mark) 显著为负 → 抛压过度 → 反转做多.",
+     "alpha_basis_contrarian_short": "Basis 显著为正 → taker 抢买过度 → 反转做空.",
+     "alpha_funding_long_bias": "Funding 高位 → 多头拥挤 → 反转偏空 (long_bias 指 funding 偏多, 信号含义是反向).",
+     "alpha_funding_short_bias": "Funding 低位/负 → 空头拥挤 → 反转偏多.",
+     "alpha_oi_long_confirming": "OI 扩张 + 价涨 → 新多资金入场 → 顺势做多.",
+     "alpha_oi_short_confirming": "OI 扩张 + 价跌 → 新空资金入场 → 顺势做空.",
+     "alpha_ls_contrarian_long": "大户 long/short ratio 偏低 → 反转做多.",
+     "alpha_ls_contrarian_short": "大户 long/short ratio 偏高 → 反转做空.",
+ }

  def build(...):
      ...
+     glossary = {
+         code: REASON_CODE_GLOSSARY[code]
+         for code in brief.baseline_reason_codes
+         if code in REASON_CODE_GLOSSARY
+     }
      payload = {
          ...
          "instructions": { "requirements": [
              ...,
+             "Use baseline_reason_code_glossary only to interpret ...; do not echo ...",
              "Return strict JSON only.",
          ]},
          "decision_brief": brief.model_dump(mode="json"),
+         "baseline_reason_code_glossary": glossary,
          "response_contract": response_contract,
      }

# aats/bootstrap/settings.py
- ai_prompt_version: str = "0.2.0"
+ ai_prompt_version: str = "0.3.0"

# configs/base.yaml
- ai_prompt_version: 0.2.0
+ ai_prompt_version: 0.3.0
```

### 5.3 测试

新增 [tests/unit/test_prompt_builder_reason_code_glossary.py](../../tests/unit/test_prompt_builder_reason_code_glossary.py)，5 用例：

1. `test_glossary_includes_only_codes_present_in_brief` — glossary 只含实际 reason_codes 对应条目
2. `test_glossary_is_empty_when_no_new_codes_present` — 无新 code 时 glossary 为空 dict
3. `test_glossary_covers_all_eight_new_p1_p2_codes` — 8 个新 code 都有语义解释
4. `test_instructions_reference_glossary_usage_policy` — instructions 包含"不要回显 glossary"规则
5. `test_glossary_does_not_alter_unrelated_payload_fields` — 不破坏 task / operating_mode / decision_brief / response_contract

验证运行：
```
./.venv/Scripts/python.exe -m pytest tests/unit/test_prompt_builder_reason_code_glossary.py -x -q
  5 passed in 0.52s
```

回归验证（全部 unit）：
```
./.venv/Scripts/python.exe -m pytest tests/unit/ -x -q
  2390 passed, 29 skipped, 6 warnings, 61 subtests passed in 174.82s
```

## 6. 给主任务的建议（超出本调研范围）

1. **Deploy 后盯 AI shadow outperformed_count 前后对比**：新 prompt 进入生产后，观察 shadow evaluation `outperformed_count / underperformed_count` 是否改善。如果 glossary 真正减少 AI 误读，AI shadow 的胜率应该略升。
2. **考虑把阈值 0.15 降到 0.10**：当前 basis 触发 3.7%、funding/oi/ls 触发 0%。数据充分后回看触发率太低会不会浪费 alpha。降阈值是下一轮 calibration 的事，不在本 commit 范围。
3. **microstructure 系列短码（`microstructure_confirms_*` / `microstructure_neutral` / `microstructure_conflicts_with_direction`）暂未加 glossary**：它们名字还算自明 + AI 模型见过"microstructure"词汇，风险较低。如果后续 shadow evaluation 显示相关 decision 质量异常可再补。
4. **未 deploy / 未 push**：等主任务 orchestration 决定何时滚。

## 7. 变更清单

| 文件 | 变更 |
|---|---|
| [aats/services/ai_service/prompt_builder.py](../../aats/services/ai_service/prompt_builder.py) | +35 行（glossary 常量 + 条件注入逻辑 + instructions 规则）|
| [aats/bootstrap/settings.py:248](../../aats/bootstrap/settings.py:248) | `ai_prompt_version: str = "0.3.0"` |
| [configs/base.yaml:128](../../configs/base.yaml:128) | `ai_prompt_version: 0.3.0` + 注释 |
| [tests/unit/test_prompt_builder_reason_code_glossary.py](../../tests/unit/test_prompt_builder_reason_code_glossary.py) | 新增 5 用例，117 行 |
| [docs/review/ai_prompt_new_reason_codes_review_2026_04_19.md](ai_prompt_new_reason_codes_review_2026_04_19.md) | 本报告 |
