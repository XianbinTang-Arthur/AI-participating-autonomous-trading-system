# FS-004 Research Factory 候选选择与封存测试集设计和实施范围

> 文档状态：Phase 3V train/valid 选择协议与 test 内容封存已实施；最终 OOS 评估、walk-forward、历史产物审计与独立复核开放  
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3V 整改  
> 核对范围：Research Factory real-data runner、Gold segment content seal、candidate/recommendation lineage、registry/manifest artifact 引用和隔离单元测试  
> 运行时边界：不读取 `.env.*`，不连接研究/交易数据库、Redis、NATS、交易所或账户，不运行历史实验，不发布参数，不部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段处理 `FS-004` 中可由仓库代码收口的选择偏差路径。原 real-data runner 构造
train/valid/test，却只在 test 上计算 factor、future return、metrics 和 gate，然后直接生成
candidate/recommendation。Phase 3V 改为：train 用于开发稳定性门，valid 用于候选选择和
对外 metrics，二者必须同时通过；test 只参与输入完整性/数据质量检查和不可逆内容指纹，
不进入 factor、label、baseline、execution metrics 或绩效选择 gate，并标记
`sealed_not_evaluated`。

本阶段不实现最终 OOS 评估，也不宣称现有 candidate 已具备独立 test 证据。没有建立一次性
解封授权、holdout view ledger、purged walk-forward、多窗口稳定性、multiple-testing 修正
或历史 artifact 重跑。新 recommendation 明示 metrics 只是 development evidence；在后续
独立 holdout 流程完成前，不能把它用于真实资金放行。

## 2. 整改前行为与根因

原版本 `research_factory_real_data_runner_v1` 的顺序是：准备 60/20/20 segments，读取
`rows_for_segment("test")`，在该段计算 factor 与 future return，生成单一 metrics，运行
deterministic gate，再写 candidate 和 ready-for-review recommendation。train/valid 仅用于
数据质量行数，没有参与选择证据。

代码没有自动超参搜索、候选排名或 live promotion，因此 Phase 2 将原 P1 降为 P2；但 test
名称与实际 validation 角色冲突，操作员重复修改 factor/threshold 后重跑会逐步污染 OOS。
根因是缺少显式选择协议、segment role、未评估 holdout 状态、内容 seal 和推荐限制声明。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `datasets/gold_bars.py` | 对 prepared segment 的精确内容生成 `rfseg_` SHA-256 seal，不计算绩效 |
| `real_data.py` | 执行 train/valid 双门、仅向 valid 合并执行证据、写 development evidence、封存 test、生成 v2 candidate lineage |
| `evidence.py` / `workflow.py` | 将执行现实性报告绑定到 `benchmark_segment=valid` 与精确 valid 时间窗并保持序列化兼容 |
| `recommendations.py` | 对 sealed-holdout candidate 的协议字段与 evidence ref 失败关闭，并披露限制 |
| `scripts/rdp_run_execution_realism.py` | 为 Phase 4 summary 写 contract/source/window/segment/dataset identity；支持精确 UTC timestamp |
| `development_evidence.json` | 记录 train/valid 各自 row count、metrics、gate、选择规则和 test seal 状态 |
| `metrics_snapshot.json` | 只保存 valid 候选选择 metrics，不能解释为 test/OOS metrics |
| `candidate_artifact.json` | 固定 selection protocol、development ref、valid benchmark 与 test seal |
| `research_recommendation.json` | 引用 development evidence，并声明 sealed test 尚未评估 |

协议版本为 `train_valid_selection_test_holdout_v2`；runner code version 升为
`research_factory_real_data_runner_v2`，防止 v1/v2 artifact 在没有迁移说明时混用。

## 4. 输入、输出与接口

`ResearchFactoryExperimentConfig` 的 train/valid/test ratio API 保持兼容。成功结果新增可选
`development_evidence_ref`，CLI JSON 因此是向后兼容的新增字段。

候选选择的固定输入/输出为：

```text
train rows -> factor/segment-local label -> metrics -> train stability gate
valid rows -> factor/segment-local label -> metrics -> valid selection gate
train PASS AND valid PASS -> candidate metrics = valid metrics
test rows -> structural/data-quality validation + content fingerprint
          -> no factor/label/performance metrics/selection gate
```

若启用 execution realism，外部 summary 必须声明 `benchmark_segment=valid`，并且
`window_start/window_end` 精确等于 valid segment。覆盖全实验窗口（包含 test）或声明 test
的 summary 会在 evidence gate 失败；执行指标只合并到 valid metrics，不进入 train gate。

segment-local label 使 horizon 超出当前 segment 的尾部行保持 `None`，不会跨 train→valid 或
valid→test 边界借用未来价格。当前仍不是完整 purged/embargoed walk-forward。

## 5. 数据库 schema、表、索引与约束

无数据库、migration、ORM、表、索引或约束变更。Research memory registry 继续写现有 JSONL
结构；candidate payload 和 manifest output refs 是向后兼容新增字段。没有读取历史 registry
或数据库来判断 test 是否曾被人工查看，因此历史污染状态保持 UNKNOWN。

## 6. 事务、一致性与并发

ExperimentRecorder 保持原子 JSON 写入和 manifest 更新顺序：先写 source/evidence，再写
development evidence 和 valid metrics；双门失败时写 failure/registry，但不写 candidate；
双门通过后才写 candidate/recommendation。

test content seal 绑定完整 dataset fingerprint、segment 名称和 prepared rows 的规范化精确
内容。相同数据确定性得到相同 seal，任一 test OHLCV/funding/metadata 变化都会改变 seal。
seal 证明内容身份，不证明数据正确、未被研究者观察或最终评估只运行一次。

## 7. 授权、认证与数据安全

- 本阶段仅改研究 artifact；不允许 runtime mutation、active parameter、OKX write 或 live order；
- 新 recommendation 仍要求 operator approval，但 approval 不能替代最终 OOS；
- seal 是内容哈希，不包含数据库 URL、凭据或账户数据；
- 后续 holdout 解封必须独立授权、只运行一次并记录 actor/reason/view count；
- 不得把“知道 test seal”解释为“没有人看过 test 数据”。

## 8. 错误处理与幂等

以下情况失败关闭：

1. train 或 valid 任一 gate 失败；failure 文本带 segment 前缀；
2. train/valid gate thresholds 不一致；
3. 请求对非 train/valid segment 执行 development evaluator；
4. test segment 为空或无法生成内容 seal；
5. sealed candidate 的 protocol、development segments、benchmark、holdout segment/status、
   content fingerprint 或 development evidence ref 不匹配；
6. development evidence 没有在 recommendation evidence refs 中逐字引用。
7. execution summary 缺少/错用 benchmark segment，或窗口覆盖 train/test；
8. execution summary 的 dataset identity 同时缺少 exact fingerprint 与显式 compatibility reason。

同一输入在独立 artifact root 得到相同 test seal 和 development metrics。现有 overwrite
行为仍由显式配置控制；registry novelty gate 继续阻止同 dataset/factor 的普通重复运行。

## 9. 状态转换与生命周期

```text
dataset/source/evidence PASS
  -> train metrics + stability gate
  -> valid metrics + selection gate
  -> write development_evidence(test=sealed_not_evaluated)
  -> both PASS ? write valid metrics/candidate/recommendation : fail without candidate
  -> shadow/paper observation and governance review
  -> future independent one-time holdout evaluation (not implemented)
  -> human production decision (not authorized)
```

新 v2 recommendation 的 `ready_for_review` 只表示可进入研究审查，不表示 test PASS、最终
OOS PASS、参数可应用或生产可用。

## 10. 缓存与性能

real-data runner 现在对 train 和 valid 各执行一次 factor/baseline，计算量较原单段 test
增加，但 test 不做绩效计算。test seal 对该段完整 prepared rows 做一次规范化 JSON hash，
时间和内存为 O(test rows)。这是可审计内容身份的代价；大窗口性能尚未 benchmark。

若未来数据量过大，应使用流式规范化 hash，同时保持现有 schema/version 和确定性 golden
tests；不能为了性能只 hash row count/min/max 而丢失内容身份。

## 11. 日志、监控与审计

`development_evidence.json` 是 v2 选择审计真源，包含：

- selection rule 与 benchmark segment；
- train/valid row count、metrics、gate 和 evaluated_at；
- test row count、content fingerprint、`metrics_exposed=false`；
- dataset fingerprint 和 created_at。

manifest、candidate、recommendation 与 memory registry 均链接该 artifact。当前没有集中
holdout access ledger、artifact view telemetry 或历史重复适配报告，这些是 FS-004 残余项。

## 12. 测试策略

对抗测试覆盖：

1. protocol/code version 与 segment roles 固定；
2. combined gate 要求 train、valid 同时通过并保留 segment 前缀；
3. 成功产物的 manifest/candidate/recommendation/development refs 闭环；
4. factor evaluator 只收到 train/valid rows，test timestamps 从未进入；
5. train 失败、valid 通过仍不得生成 candidate；
6. 只改变 test 内容不会改变 development metrics，但一定改变 test content seal；
7. recommendation 缺少匹配 development ref 时失败关闭；
8. sealed test 尚未评估的限制进入 recommendation。
9. 覆盖完整实验窗口的 execution summary 和 `benchmark_segment=test` 均失败关闭；
10. execution metrics 只影响 valid，不覆盖 train development metrics；Phase 4 writer 的
    segment/dataset identity 参数组合不允许模糊或冲突。

仍需历史 artifact/registry 只读审计、最终 OOS runner、one-time access ledger、walk-forward、
multiple-testing correction 与 independent reviewer。

## 13. 迁移、回滚与兼容

v1 artifact 不原地升级，也不能因 v2 代码存在而追认为 train/valid 合格或 test 未污染。
历史 `benchmark_segment=test` candidate 保持其当时证据，必须单独盘点、失效或重跑。

回滚到 v1 会重新用 test 选择 candidate，恢复已确认的偏差路径，因此不是安全回滚。若 v2
导致研究吞吐下降，应修复 v2 或停止候选生成，不能静默回退 test 选择。下游解析新增字段
应保持兼容，但 production gate 必须识别 v2 的“holdout 未评估”限制。

## 14. 配置与环境隔离

train/valid/test ratio 仍由 experiment config 提供且必须均为正、和为 1。Phase 3V 不新增
可关闭 sealed holdout 的开关，也没有允许把 benchmark 改回 test 的配置。真实 Gold 数据
加载仍由调用方注入 data source；本阶段测试只用内存 fixture。

研究 artifact 必须位于 `artifacts/research`。没有连接 runtime active parameter、managed
profile 或 live 数据库；静态/单元证据不能证明历史数据质量和真实 artifact lineage。

## 15. 代码组织与依赖

不引入第三方依赖。内容 seal 复用标准库 SHA-256 和 Gold dataset 已有规范化规则；选择逻辑
留在 real-data runner，避免把 smoke/synthetic runner 的测试用途与真实数据资本证据混同。

推荐构建器只对携带 `holdout_status` 的新协议候选施加 v2 完整性校验，旧 candidate fixture
和历史读取保持兼容；这是一条明确版本边界，不把历史缺字段伪装为 v2。

## 16. 文档、运维手册与验收标准

Phase 3V 仓库内验收标准：

- real-data candidate 不再读取 test 计算 factor/label/绩效 metrics/selection gate；test 只保留
  dataset integrity/quality 与内容 seal 路径；
- train/valid 双门和 valid benchmark 有单元证据；
- test exact content seal、manifest/candidate/recommendation lineage 闭环；
- execution realism 只允许精确 valid segment evidence，不能经全窗口 summary 间接消费 test；
- v2 recommendation 明示 holdout 未评估；
- focused tests、应用/全仓 Ruff、完整 unit、链接/YAML/diff 通过；
- 历史审计、最终 OOS、walk-forward、多重检验和独立复核继续登记 OPEN；
- 真实资金生产继续 `NO-GO`。

当前裁定：
`PARTIALLY REMEDIATED / TEST SEALED FROM CANDIDATE SELECTION / FINAL OOS & HISTORY AUDIT OPEN`。

Phase 3V 本地静态/隔离验收结果：FS-004 focused `57 passed`，Research Factory + Phase 4
contract `327 passed`，strict marker `4439 tests collected`；仓库内独立 basetemp 完整 unit
为 `4409 passed, 30 skipped, 1660 warnings, 85 subtests passed in 115.95s`。应用/全仓 Ruff、
dependency/connection verifier、pip check、16 份 YAML、789 份 Markdown 的 1,049 个本地
目标和 diff check 均通过。原样 unit 命令在 87 项后被 Windows 系统 Temp ACL 阻断；该
fixture setup 错误未被表述为业务断言成功。未运行任何数据库、WSL2、历史实验或 live 验证。

实施证据见
[`../../audit/full_system_2026_08_24/42-fs-004-research-selection-holdout.md`](../../audit/full_system_2026_08_24/42-fs-004-research-selection-holdout.md)。
