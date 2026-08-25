# 03 量化、行情、研究与回测审查

## FS-003 — 回测使用同一根 K 线的信息并在同一收盘价成交

- 严重度：P1；置信度：高；类别：lookahead / execution timing
- 状态：VERIFIED
- 位置：`aats/data_platform/replay/adapters/independent_adapter.py:91-100,196-251`；`aats/data_platform/replay/backtest/harness.py:228-248,315-320`；`fill_simulator.py:205-228`
- 证据：adapter 先把当前 bar 加入 history，使用本 bar 的 close-open 收益、close、high/low、volume 计算分数和动作；harness 紧接着以同一 `bar.close` 调用成交模拟器。IOC 只加固定 1 bps 滑点。
- 触发：任何当前 bar 形成开/平仓动作的回测。
- 后果：策略在收盘信息已知后仍获得该收盘成交价，系统性高估可实现收益并低估延迟/跳空。
- 复现推理：时间顺序由同一 for-loop 和相同 bar 对象直接证明；单测明确锁定 post-only/bounded-limit 按 bar close 成交。
- 建议：定义 decision timestamp 与 tradable timestamp；默认在下一 bar open/可观察 quote 成交，或引入可配置延迟和订单簿事件。旧结果必须标记模型版本并失效重跑。

### Phase 3C 当前状态补充

原 `VERIFIED` 条目是 Phase 1/2 的修复前事实。Phase 3C 已固定 `next_bar_event_v2`，把 observation complete/decision/submit 与下一 tradable/fill 事件拆开；单 bar、gap、unfinished、duplicate/overlap、post-only no-liquidity、partial fill 和旧模型重启路径均有确定性测试。原 same-bar-close 利用链现在得到 `0 fills + expired_no_next_event`。FS-003 当前为 **CODE REMEDIATED / REVIEW & EVIDENCE RE-RUN OPEN**；旧 artifact 清单/重跑、独立复核以及 FS-014 的订单簿现实性尚未完成。权威当前证据见 [23-fs-003-backtest-causal-timing-remediation.md](23-fs-003-backtest-causal-timing-remediation.md)。

## FS-004 — test 段承担候选选择，train/valid 未参与证据形成

- 严重度：P1；置信度：高；类别：selection bias / research governance
- 状态：VERIFIED
- 位置：`real_data.py:121-123,491-528`；`datasets/segments.py:13-41`
- 证据：系统构造 60/20/20 train/valid/test，但 runner 只取 `rows_for_segment("test")`，在 test 上计算 factor、future return、metrics、gate，并直接生成 candidate/recommendation。代码中没有消费 train/valid 行的评估路径。
- Phase 2 现行裁定：**P2 / DOWNGRADED**。事实缺陷成立，但当前具体函数没有训练、超参/特征搜索、自动排名或自动 live promotion；首轮“直接选模”的严重度推断过强。历史重复人工适配仍需 artifact/lineage 审计。完整反证见 `17-p1-adversarial-verification.md`。
- 触发：用 Research Factory 比较、迭代或筛选多个 factor proposal。
- 后果：test 被反复用于选择后不再是独立样本；推荐的 OOS 语义被污染。
- 建议：train 用于拟合/初筛，valid 用于超参/候选选择，test 仅一次性最终评估；保存 proposal lineage 和试验次数，增加 purged walk-forward/cross-window 证据。

## FS-014 — 成交与成本模型不足以支撑 live 收益外推

- 严重度：P2；置信度：高；类别：market realism
- 状态：VERIFIED
- 位置：`fill_simulator.py:21-48,140-148,205-310`；`harness.py:339-358`；`research_factory/benchmarks/baseline.py:116-144`
- 证据：IOC 永远全成，post-only 用固定分段概率且成交即全量，bounded-limit 永远全成且固定混合费率；没有队列位置、限价触达、部分成交、撤单、延迟、盘口深度、冲击或波动状态。CostValidator 的 actual cost 仅传 `fee_bps`，注释承认未计细化滑点。
- Phase 3C 边界：harness 已能正确消费对抗性 partial fill，并阻断 same-bar timing；默认 FillSimulator 仍不会根据真实盘口生成 partial fill，IOC/bounded 的全成与 post-only bar proxy 缺口仍成立。
- 触发：用该 backtest/scorecard 估算实盘容量、成交率或净收益。
- 后果：回测—实盘差异不可量化；小边际策略尤其容易由正转负。
- 建议：按证据等级区分 factor-only、bar proxy、L2/event replay；上线 gate 只接受与订单类型、symbol、时段、容量相匹配的成本模型，并用真实 fill 做校准和保守置信区间。

### Phase 3N 当前状态补充

原 `VERIFIED` 条目保留 Phase 1/2 修复前事实。Phase 3N 已固定
`ohlcv_participation_cap_v2`：IOC/post-only/bounded-limit 都要求正 volume 并受
默认 1% participation cap，支持 partial fill；IOC/bounded 的 next-open 流动性只取
已经闭合的 observation volume，bounded 按 taker fee + fixed slippage，成本诊断记录
fee+slippage。scorecard 明示 OHLCV 与无 L2/queue/spread/impact/latency 校准。

原“无量全成、命中全成、混合费率零滑点、成本漏记”路径已经收敛，但 bar-volume
proxy 仍不是真实撮合校准，历史 artifact/策略尚未重跑，独立复核未完成。FS-014
更新为 **PARTIALLY REMEDIATED / OHLCV CONTAINED / L2 CALIBRATION OPEN**；G3
仍未放行。权威证据见
[34-fs-014-ohlcv-fill-realism-containment.md](34-fs-014-ohlcv-fill-realism-containment.md)。

## FS-015 — replay 与生产仍保留已知 short-bias 行为差异

- 严重度：P3；置信度：高；类别：backtest/live parity
- 状态：VERIFIED，但当前 derivatives tracked profile 下为 dormant
- 位置：`aats/services/strategy_engines/independent/scoring.py:209-217`；`independent_adapter.py`
- 证据：生产代码注释明确记录：`short_bias_enabled=False` 时生产 short leg 为 0，replay 仍可能选择 short dominant leg。当前 derivatives/derivatives_live YAML 设置 true，因此当前默认不触发；配置变更后会触发。
- 建议：在 replay 参数中加入同源 gate，并建立生产/回放 golden vector 对照测试；禁止用注释接受永久差异。

### Phase 3R 当前状态补充

原 `VERIFIED-dormant` 条目保留 Phase 1/2 修复前事实。Phase 3R 在
`ReplayParameterOverrides` 增加生产同名 strict boolean，并通过 CLI/from-dict/to-dict
传播和固化；independent adapter 在 score history、dominant-leg 和状态机之前执行 gate，
关闭时 short score 恒为 `0.0`。生产/replay golden vector、真实 bearish vector与
非布尔失败关闭均已有隔离测试。

这不使两端输入因子、AI assessment 或成交模型完全等价，也没有重跑历史 artifact。
FS-015 更新为 **CODE REMEDIATED / HISTORICAL EVIDENCE RE-RUN & INDEPENDENT REVIEW OPEN**；
权威当前证据见
[38-fs-015-replay-short-bias-parity.md](38-fs-015-replay-short-bias-parity.md)。

## 市场数据与泄漏控制的良好证据

### Phase 3V FS-004 当前状态补充

原 FS-004 条目保留 Phase 1/2 修复前事实。Phase 3V 将 real-data runner 升级为
`train_valid_selection_test_holdout_v2`：train 与 valid 分段计算 factor、segment-local
label、metrics 与 gate，并要求二者都 PASS；valid 是 candidate benchmark。test 仍参与
输入质量/来源一致性 gate，但不进入这些绩效评价函数；它生成绑定 prepared rows 与 dataset fingerprint 的内容 seal，并在
development evidence 中标记 `sealed_not_evaluated`/`metrics_exposed=false`。

新 candidate/recommendation 会校验 protocol、segment roles、development evidence ref
和 holdout seal，并披露 metrics 只是 development evidence。该实现不证明历史 v1 test
未被反复查看，也没有最终一次性 OOS、purged walk-forward、多重检验或独立复核，因此
FS-004 更新为 **PARTIALLY REMEDIATED / TEST SEALED FROM CANDIDATE SELECTION /
FINAL OOS & HISTORY AUDIT OPEN**。权威当前证据见
[42-fs-004-research-selection-holdout.md](42-fs-004-research-selection-holdout.md)。

- Gold bar 记录校验正价格、OHLC 关系、非负 volume、时区、symbol/timeframe、重复时间戳。
- 数据按时间排序，切片是半开区间，train/valid/test 不重叠；future Ref 偏移在 AST parser 和 evaluator 双层拒绝。
- dataset fingerprint 包含窗口、segment、source watermark、processor version，绝对路径被归一化为文件名，利于复现。
- 数据质量 gate 跟踪 gap、funding 缺失、来源版本一致性和证据引用。

## 未知项

- 真实 Gold 表是否只包含闭合 bar、交易所时间与本地时钟偏移、缺失/重复数据在当前 live 库的实际比例。
- 真实 fee tier、maker rebate、funding boundary、合约乘数、最小下单量和滑点分布。
- 历史研究是否反复查看 test 后调参；代码不能证明人的研究流程。
- 真实成交回灌对成本模型的校准频率和漂移告警是否有效。
