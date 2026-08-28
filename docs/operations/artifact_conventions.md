# Artifact 规范 (Artifact Conventions)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 最后核对：2026-08-28（起始 HEAD `c15ccd2d5057`，含当前 Step 2/3 provenance、参数导入及 Phase 3/4 子产物语义收口候选；以本文档所在 HEAD 为准）。Artifact 是证据/审计产物，不等于 runtime 配置真源；尤其 active parameter JSON 不再被主交易 loader fallback 读取。


## 1. 目录结构

```
artifacts/
  research/
    experiments/{uuid}/           # Step 1/2 单实验
      diagnostics.json            # 核心诊断数据
      replay_decisions.csv        # Replay 决策记录
      report.md                   # Markdown 报告
      replay_params_used.json     # (可选) 实际使用的参数快照
      parameter_recommendations.json  # (Step 1 产出)
      parameter_candidates.json       # (Step 2 产出)
      {uuid}/                     # 参数扫描子实验
        diagnostics.json
        replay_decisions.csv
        report.md
      comparison_summary.json     # (参数扫描产出)
      comparison_report.md        # (参数扫描产出)

    calibration_batches/{batch_id}/  # Step 1 批量校准
      round_manifest.json
      ...

    calibration_rounds/{round_id}/   # Step 1 单次校准（legacy phase2_step1 命名）
      round_manifest.json
      ...

    step2_rounds/{round_id}/         # Step 2 正式研究 round
      parameter_candidates.json
      round_manifest.json
      round_result.json              # 不可变跨阶段 result ref
      ...

    step3_rounds/{round_id}/         # Step 3 扩展扫描与合并参数 round
      parameter_candidates_merged.json
      round_manifest.json
      round_result.json              # 不可变跨阶段 result ref
      ...

    attribution_rounds/{round_id}/   # Phase 3 归因 round
      round_manifest.json
      round_result.json              # 不可变跨阶段 result ref
      per_combo/
        {combo_key}/
          result_{uuid}.json          # 不可变 child result sidecar
          {run_id}/
            attribution_summary.json
            replay_live_alignment.csv
            top_failure_modes.json
            replay_params_used.json
            live_attribution_report.md
      family_timeframe_attribution_summary.csv
      phase3_live_attribution_conclusion.md

    execution_rounds/{round_id}/     # Phase 4 执行评估 round
      round_manifest.json
      round_result.json              # 不可变跨阶段 result ref
      per_combo/
        {combo_key}/
          result_{uuid}.json          # 不可变 child result sidecar
          {run_id}/
            execution_alignment.csv
            fill_feasibility_summary.csv
            slippage_summary.csv
            execution_cost_summary.json
            replay_params_used.json
            live_execution_realism_report.md
      execution_realism_comparison.csv
      phase4_execution_realism_conclusion.md

  governance/
    artifact_index.json
    active_round_index.json
    current_parameter_registry.json      # → DB: governance.parameter_sets
    quality_monitor_summary.json

  decision_system/
    recommendation_registry.json          # → DB: governance.recommendations
    active_decision_registry.json         # → DB: governance.active_decisions
    evidence_bundle_index.json
    parameter_apply_history.json          # → DB: governance.parameter_apply_history

configs/active_parameter_sets/
    active_parameter_registry.json        # 历史兼容/审计副本；runtime 不读取
    <family>_<timeframe>.json             # 历史 per-combo 文件副本
```

> 标注 `→ DB:` 的治理 registry 通常同时有 governance schema 表；具体读写/降级语义以对应模块为准。runtime active parameters 是例外：`governance.active_parameter_sets` 为唯一真源，不从上述 JSON fallback。

---

## 2. Round ID 格式

```
{YYYYMMDD}_{HHMMSS}_{uuid8}
```

示例: `20260403_143052_a1b2c3d4`

- 时间为 UTC
- uuid 取前 8 个 hex 字符

---

## 3. Round Manifest 统一规范

每个 round 目录**必须**包含 `round_manifest.json`，字段规范:

```json
{
  "round_id": "20260403_143052_a1b2c3d4",
  "phase": "phase3",
  "status": "succeeded",
  "started_at": "2026-04-03T14:30:52.123456+00:00",
  "finished_at": "2026-04-03T14:35:12.654321+00:00",
  "scope": {
    "symbol": "BTC-USDT-SWAP",
    "families": ["independent", "directional"],
    "timeframes": ["15m", "1h"],
    "window": {"start": "2026-03-31", "end": "2026-04-02"}
  },
  "input_refs": {
    "dataset_version": "v1.0",
    "parameter_set_id": null
  },
  "output_refs": {
    "summary_path": "family_timeframe_attribution_summary.csv",
    "report_path": "phase3_live_attribution_conclusion.md"
  },
  "combos": [
    {
      "key": "independent_15m",
      "family": "independent",
      "timeframe": "15m",
      "status": "succeeded",
      "run_dir": "per_combo/..."
    }
  ],
  "code_version": null,
  "notes": null
}
```

### 3.1 必须字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `round_id` | string | 唯一标识 |
| `status` | string | 整体状态 |
| `started_at` | string | ISO 8601 UTC |
| `finished_at` | string | ISO 8601 UTC |
| `scope` | object | 范围描述 |

### 3.2 推荐字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `phase` | string | Phase 标识 |
| `input_refs` | object | 输入引用 |
| `output_refs` | object | 输出引用 |
| `code_version` | string | 代码版本 |
| `notes` | string | 备注 |

### 3.3 status 合法值

见 [Round 生命周期](round_lifecycle.md)。

Phase 3/4 manifest 同时保留 `overall_status` 作为旧 evidence reader 的兼容投影；统一规范与新
validator 使用 `status`。两者必须由同一计算结果生成，不能独立编辑。多 combo round 的 scope
使用 `families` / `timeframes` 数组，不伪装成单一 family/timeframe。

### 3.4 Step 2 基线契约

当前 Step 2 生产者分别发布 `aats.step2_candidates.v1` candidate 和
`aats.step2_round.v1` manifest，manifest 最后以不可变方式落盘。两者必须绑定同一正式 round ID、
symbol、dataset version、combo keys/count 和非空 UTC 研究窗口；manifest 还必须绑定 candidate 的
SHA-256 与精确字节数。`succeeded` 只适用于三项预期 calibration 与四项 formal scan 身份精确、
唯一且逐项成功、存在显式 start/end、candidate
恰含 `independent_1h`、`directional_15m`、`directional_1h` 且没有 pending validation 的 round；
其他仍有可审计产物的情况只能是 `partial_success`，全部失败才是 `failed`。

Step 3 显式指定一个 Step 2 目录时，该目录缺失或任何契约不一致均失败关闭，不能回退默认值或
其他 round。自动选择时只审查时间最新的正式目录组；最新组无效、同一 `started_at` 并列或存在
非标准目录时同样失败，不能静默吃旧基线。未找到任何 Step 2 round 时只可形成 provenance
`status=missing` 的审计/partial 结果，绝不能发布 `succeeded` Step 3。

Step 2 完成持久化后还会在 stdout 唯一输出 `RDP_STEP2_RESULT_JSON=` 运行级 marker，绑定本次
子进程的 round 目录、candidate 绝对路径、SHA-256、status、symbol、dataset 与 window。在 managed
DB 环境，必须先成功写入 exact Step 2 snapshot，再写入并输出该 marker；snapshot 失败时退出非零且
不生成 `round_result.json`。未配置 managed DB 时仅保留显式离线文件模式。完整
pipeline 实时转发日志，只保留唯一 marker；缺失、重复、状态不符、路径越界或 digest 不符都会把
该阶段改判为失败，不能再扫描全局“最新目录”猜测本轮产物。

Phase 6 消费 legacy Step 2 审计证据时也必须显式指定本轮 exact Step 2 round ID。evidence bundle
记录治理 DB snapshot 的完整 typed fingerprint，promotion consumer 再与目标 Step 3 managed snapshot
中的 `step2_round_id` / `step2_snapshot_sha256` 比较。先生成 Step 3-A、随后又产生 Step 2-B 时，
Step 2-B 不能因成为 `latest` 而替换 A 的父证据；指定 snapshot 缺失、来源降级或指纹漂移均保持 hold。
目标 combo 的资本晋级还受统一 Phase 2 hard gate 约束：`experiments_with_openings >= 1`、
`max_opening_count >= 1`、`mean_positive_edge_ratio >= 0.20`。这些是必要条件而非可由其他维度补偿的
评分项；其他 combo 的结果只属于其自身，不能让低 edge 目标通过 readiness 或 apply qualification。

当前 legacy Step 2 calibration/scan 是参数探索与审计产物，不是 SWAP 资本晋级证据。没有独立、
manifest-bound derivatives qualification bundle 时，Phase 2 汇总必须报告
`derivatives_phase2_promotion_evidence_unavailable` 并保持 hold；不得把 SPOT `backtest-run/v2`、
手工 metrics 或文件内部自洽哈希提升为永续合约资格真值。后续正式生产边界见现行
[衍生品 Phase 2 晋级证据任务书](../task/rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md)。

### 3.5 Step 3 可导入参数契约

`step3_rounds/<round_id>/parameter_candidates_merged.json` 是治理候选输入，不是可随意覆盖的
临时文件。当前生产者发布 `aats.step3_candidates.v1` candidate 与
`aats.step3_round.v1` manifest：candidate 显式携带 `round_id`、`dataset_version`、
`scope.symbol` 和 `candidates`；manifest 最后不可变发布，并携带 `phase=step3`、整体 status、
UTC `started_at/finished_at`、scope/input refs，以及 candidate 的 SHA-256 和字节数。

在已配置 managed governance DB 的环境中，文件 manifest 不是最终信任根。生产者必须在输出
`RDP_STEP3_RESULT_JSON=` 之前插入一次 `phase2_step3` research round snapshot；snapshot 逐字保存
candidate/manifest UTF-8 内容及其 SHA-256/字节数，并绑定精确 Step 2 managed snapshot 的完整身份
指纹。相同 round ID 仅允许所有 canonical 字段完全一致的重试；snapshot 缺失、冲突、DB 不可达或
Step 2 父 snapshot 不一致时，不输出成功 marker，也不允许导入。受管 importer、Phase 3/4 lineage
与 promotion consumer 均会把磁盘文件和该 exact DB snapshot 再次比较；同步改写 candidate、manifest
及文件内哈希仍会被拒绝。未配置 managed DB 的本地文件兼容仅供开发/审计，不能自行取得资本晋级资格。

`succeeded` Step 3 还必须同时满足：两个预期 expanded calibration 身份精确、唯一、均成功且
未跳过、使用经上述
契约验证的 `succeeded` Step 2、完成 merge、恰好产生四个非空 combo、没有 pending/default
证据，并且 constraint check 无 violation、无 auto-fix。任何可用但不完整的结果只能标记
`partial_success`；不能借“管线进程退出 0”把不完整研究包装成完整候选。

自动导入逐项绑定目录、两份 schema、round、symbol、dataset version、digest 和 size，并复核
Step 3 calibration 及其引用 Step 2 的 calibration/scan 身份集合精确且无重复；任何不一致
均返回 `round_metadata_invalid` 且不写治理层。`failed` round 不可导入；`partial_success` 最多作为
`draft` 导入且不 supersede 现有 candidate。只有完整 `succeeded` round 才能在全局 DB advisory
lock 内先以 draft 插入/核验完整集合，再在单一事务中整体公开为 candidate；决策读端不会看到
崩溃恢复留下的 candidate 前缀。同 artifact 重试会从 DB 重读真实生命周期，已推进到
frozen/released/deprecated 的成员不会回退或在返回值/JSON 镜像中伪装成 candidate。

Step 3 在上述 managed snapshot 发布成功后才唯一输出 `RDP_STEP3_RESULT_JSON=`，除本轮 merged candidate 身份外还绑定它实际消费的
Step 2 round/digest。完整 pipeline 必须证明该引用与本次 Step 2 marker 一致，后续 Phase 3/4 与
import 只接收这份精确 candidate；即使另一个并发 pipeline 发布了字典序或时间上更“新”的 round，
也不得切换。显式续跑且本次未执行 Step 3 时才保留“选择现有最新可信 round”的兼容语义。调用方
可向 importer 传入精确 candidate；该路径必须恰位于正式
`step3_rounds/<round_id>/parameter_candidates_merged.json`，否则返回 `round_metadata_invalid`。

自动 supersession 只由当前仍为 candidate 的 canonical 新成员驱动，并仅替换同
family/symbol/timeframe、同 Step 3 来源且经旧/new manifest 证明时间更早的旧 candidate。旧候选
废弃与 replacement 状态检查共用资本 apply combo 事务锁；若 replacement 已推进或 apply 抢先，
保留旧候选并报告状态冲突。最新 round 尚未完成时不得回退导入旧 round。

### 3.6 Phase 3/4 子运行结果契约

Phase 3 attribution 与 Phase 4 execution realism 在消费正式 Step 3 candidate 前，先验证 candidate、
manifest、scope、window 和 SHA-256，并分别计算候选原始参数与 family defaults 合并后的 resolved
参数指纹。旧式平坦参数文件仍可用于离线诊断，但其 lineage 明确为 `unbound`，不能取得 promotion
资格。

每个 round 的每个 family/timeframe 使用独立的
`per_combo/<combo_key>/` 根目录；父进程为每次 one-shot 调用生成唯一 `result_<uuid>.json`，子进程
通过不可变写入发布 `aats.live_attribution_result.v1` 或
`aats.execution_realism_result.v1` sidecar，并在 stdout 输出唯一稳定 marker。父进程必须同时验证：

- marker 与 sidecar 内容完全相同，exit code 与 status 一致；
- family、symbol、timeframe、dataset、window、replay/fee scope 与调用参数一致；
- `finished_at` 是显式 UTC，run ID/path 位于当前 combo 根且全路径无符号链接；
- resolved 参数指纹与正式 Step 3 combo 一致；
- 每一项被消费的 JSON/CSV/report 路径、SHA-256 和精确字节数均与 sidecar 一致；文件通过描述符
  读取，读取前后身份、大小和修改时间必须稳定。

父进程只解析已经完成上述校验的内存字节，不再通过执行前后目录差、字典序、时间戳或 global
latest 推断子 run。并发运行、同秒启动或污染目录因此不能把另一进程的 attribution/execution
结果拼入当前 round；sidecar 缺失、重复 marker、路径越界、digest/参数漂移、JSON schema/type
错误、CSV 非法 UTF-8 或解析错误均只能把当前 combo 标记为 failed。Phase 3 还会复核完整表头、
scope/UTC 窗口、alignment/taxonomy 和 aligned live lineage，并从明细重算 attribution summary 与
failure modes；Phase 4 会复核完整表头、候选/市场对齐、有限数值和 OHLC/notional 关系，再复用正式
feasibility、slippage 与 execution-cost 模型重算明细和摘要。任何业务字段或明细/汇总不一致都失败
关闭，不能把“哈希正确”或“可被 CSV parser 读取”等同于研究证据合格。

Phase 3/4 父进程在所有 combo 收口后，先不可变发布 `round_manifest.json`。在 managed DB 环境，
必须成功持久化绑定该 manifest 与 combo 汇总的 exact snapshot，随后才发布 `round_result.json`；
snapshot 失败时退出非零且不生成或输出 result marker。未配置 managed DB 时仅允许离线文件降级。
`round_result.json` 绑定 manifest 的绝对路径、SHA-256、精确字节数、本轮 scope、status/exit code，
以及实际消费的 Step 3 round 与 candidate digest。完整 pipeline 只接受本次子进程 stdout 中
唯一 marker 所指向的这份 result ref，并逐项复核四个 combo、Step 3 lineage 与 manifest digest；
显式续跑 Phase 5/Decision 时也必须提供精确 Step 3、Phase 3、Phase 4 result refs，禁止扫描 latest
拼接不同轮次。正式决策证据只接受 managed governance DB 中与显式 round ID 精确匹配的 Phase 3/4
terminal snapshot；文件 bootstrap 仅供审计展示，不能取得 promotion 资格。

---

## 4. 关键文件说明

### 4.1 diagnostics.json (Step 1/2)

| 字段 | 说明 |
|------|------|
| `total_bars` | 总 bar 数 |
| `opening_count` | 开仓次数 |
| `positive_edge_ratio` | 正 edge 比例 |
| `selectable_ratio` | 可选比例 |
| `execution_compatible_ratio` | 执行兼容比例 |
| `top_blocking_reasons` | 阻断原因 Top N |

### 4.2 attribution_summary.json (Phase 3)

每个 combo 的 replay vs live 归因汇总。

### 4.3 execution_cost_summary.json (Phase 4)

Phase 3V 起，若该 artifact 将被 Research Factory real-data v2 消费，除指标外还必须包含
`schema_version=execution_cost_summary_v1`、`source_run_id`、symbol/timeframe、精确 UTC
`window_start/window_end`、`benchmark_segment=valid`，以及 exact `dataset_fingerprint` 或
经审查的 compatibility reason。窗口必须精确等于 experiment 的 valid segment；覆盖完整
train/valid/test 窗口或标为 test 会失败关闭。standalone Phase 4 summary 不自动具备研究
候选证据资格。

| 字段 | 说明 |
|------|------|
| `total_candidates` | 候选订单总数 |
| `full_fill_ratio` | 完全可成交比例 |
| `slippage.mean` | 平均滑点 (bps) |
| `total_execution_cost.mean` | 平均总执行成本 (bps) |
| `cost_adjusted_edge.mean` | 成本调整后 edge (bps) |
| `positive_edge_ratio` | 正成本调整 edge 比例 |

---

## 5. 校验工具

`rdp_validate_artifacts.py` 是只读校验器：

```bash
python scripts/rdp_validate_artifacts.py
python scripts/rdp_validate_artifacts.py --phase phase3
```

`--fix` 已失败关闭，禁止原地覆盖历史 `round_manifest.json`。legacy manifest 需要迁移时，必须复制
或生成新的 artifact/round，重新计算 digest 并同步 index，经 code review 后再发布；不能把旧证据
就地“补字段”。

使用 `rdp_build_artifact_index.py` 构建索引:

```bash
python scripts/rdp_build_artifact_index.py
```
