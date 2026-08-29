# ADR 增补：LF-B1.2-A2 正式事件源与密封决策输入边界

> 文档状态：设计提案（Proposed）；A2a 已形成 verification-only 代码切片但不得接 reducer，A2b 仍仅为设计；决策算法与仓位规模需实名 RDP Owner / Independent Risk Reviewer 批准
> 最后核对：2026-08-29（代码基线 `main@a22e72d4`；运行时事实仍以现行部署证据为准）
> 上位 ADR：[`rdp_derivatives_backtest_run_v1_adr_2026_08_28.md`](rdp_derivatives_backtest_run_v1_adr_2026_08_28.md)
> 实施任务书：[`../task/rdp_derivatives_backtest_lfb1_2_engine_evidence_sow_2026_08_28.md`](../task/rdp_derivatives_backtest_lfb1_2_engine_evidence_sow_2026_08_28.md)
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO；CAPITAL PROMOTION: NO-GO**

## 1. 决策背景

LF-B1.2-A2 现有代码只完成了 `derivatives-event-set/v1`、六类事件、stream ref、integrity summary 与 restart cursor
的不可变内存合同。它尚未从文件系统读取 manifest 或 JSONL，也未从真实字节重算 producer 声明。因此：

1. 调用方仍可构造一组内部自洽、但并未由原始文件证明的 manifest/stream 对象；
2. 现有 merge API 接受裸事件 iterable，会丢失 byte offset、raw/semantic prefix 与 committed cursor；
3. `BarCloseEventV1.feature_ref` 只有内容身份，没有可验证 locator、feature manifest 或 parameter snapshot；
4. 现有 `IndependentReplayAdapter` 含 float、mutable deque 与隐式状态，不满足正式回放要求；
5. 上位 ADR 尚未冻结正式策略的评分公式与目标仓位规模，代码不得自行猜测。

本增补把 A2 拆成两个可独立验收的边界：

- **A2a — formal event source**：从 immutable bytes 建立可重启、metadata-preserving、双遍验证的六流事件源；
- **A2b — sealed decision input**：从独立的 feature/parameter artifact 建立 Decimal-only、无隐藏状态的决策输入和纯函数。

A2a 不依赖 A2b 的业务算法，可以立即实施；A2b 的数据合同可以实施，但产生 order 的算法必须在第 9 节待决项获批后解锁。

## 2. 统一安全边界

1. 所有入口保持 Python internal；不新增 CLI、HTTP、数据库、后台任务、网络或 live runtime 接口。
2. 所有 artifact root 必须是调用方显式提供的绝对 canonical 目录；禁止环境变量、`latest`、目录扫描推断或数据库回退。
3. 路径仅用于定位，身份由 raw byte size、SHA-256、canonical schema、semantic digest 与 source lineage 共同确定。
4. 所有失败均在任何经济状态 mutation、ledger append、publisher 或 checkpoint commit 前发生。
5. A2 输出固定为 `synthetic_test_only`、`capital_promotion_eligible=false`；不能解锁 Phase 6、recommendation 或 live。
6. Python frozen dataclass 不是权限凭证。每个消费边界必须从 raw bytes 重新解析并重算身份，拒绝被 `dataclasses.replace()`
   携带的伪造派生对象。

## 3. A2a 组件模型

新增内部模块 `event_source.py`，固定包含以下职责：

| 组件 | 职责 | 禁止事项 |
| --- | --- | --- |
| `preflight_non_promotable_derivatives_event_set()` | 第一遍读取 manifest、六流与 snapshot catalog，返回完整预检证据 | 不返回可直接修改的裸列表；不产生经济状态 |
| `PreflightedDerivativesEventSourceV1` | 冻结 manifest identity、各流最终 cursor、文件身份与 snapshot segment identity | 不缓存未来经济状态；不把 inode 当业务身份 |
| `DerivativesEventVerificationPassV1` | 第二遍重新打开相同 artifact，从 exact cursor 继续，并在 drain 后再次证明完整身份 | verification-only；不得驱动 reducer、checkpoint 或 publisher |
| `UncommittedDerivativeEventRecordV1` | 携带已重验 event 与该完整 LF record 后的 read cursor | 明确不是 committed cursor；不得产生经济 mutation |

第一遍和第二遍必须是两个独立 file-descriptor 生命周期。第一遍通过只代表“该时点读取到一份完整候选”；第二遍必须
在执行期间重新验证 raw/semantic identity。两遍之间或读取期间发生路径、父目录、文件对象、大小或内容漂移，统一失败为
稳定的 `event_set_identity_changed`，不得尝试自动恢复到新文件。

## 4. Manifest 读取合同

读取顺序固定为：

1. 验证 artifact root 为绝对、已存在、非 symlink/reparse/junction 的目录，冻结 root metadata；
2. 逐段 exact-case 解析 `manifest_relative_path`，拒绝 escape、大小写漂移、非普通文件和任何链接/重解析点；
3. 在 JSON parse 前以 descriptor stat 和 bounded read 执行 `<=4 MiB` gate；
4. 单描述符读取期间复核 file 与 parent identity，随后校验 exact size 和 raw SHA-256；
5. 使用 strict UTF-8 JSON decoder，拒绝 BOM、重复 key、NaN/Infinity、未知/缺失字段及非 object 顶层；
6. 要求 raw bytes 精确等于 `canonical_typed_json_bytes(parsed)`；manifest 不允许尾随 LF；
7. 通过 `DerivativesEventSetManifestV1.from_dict()` 和 `DerivativesEventSetRefV1.validate_manifest()` 双重重验；
8. 在打开任何 stream 前复核 root 未改变。

manifest 成功对象只证明 manifest 自身，不能替代六流 raw preflight。

## 5. 六流 JSONL 第一遍预检

每条 stream 必须使用单描述符、有限缓冲的顺序读取。读取器不得把最大 512 MiB 文件整体载入内存。固定要求：

- 单行（包含结尾 LF）不超过 1 MiB；单流不超过 512 MiB；单流不超过 5,000,000 条；总事件不超过 10,000,000 条；
- 文件只能是 canonical UTF-8 JSON object JSONL；拒绝 BOM、CRLF、裸 CR、空行、无最终 LF、部分 UTF-8、重复 key、
  NaN/Infinity、未知/缺失字段及任何非 canonical bytes；
- canonical record 定义为 `canonical_typed_json_bytes(event.to_dict()) + b"\n"`；
- 每行通过 `parse_derivative_replay_event()`，随后重验 event type、expected stream ID、event ID、source record ref；
- `event.ts` 必须位于 stream coverage `[warmup_start_ts,end_ts)`；source key `(ts,source_sequence)` 严格递增；
- contract/funding/tradable/bar 每个 timestamp 最多一条；mark/index 允许同 timestamp 且 sequence 严格递增；
- 重算 raw byte count/SHA-256、event count、semantic event digest、first/last key、source registry ID 集合、parent raw
  partition SHA-256 集合和 integrity evidence；逐项 exact 比较 stream ref；
- 每个 record 后产生 cursor：stream fingerprint、下一 byte offset、已读 count、raw prefix SHA-256、semantic prefix SHA-256
  和 last key。只有完整 canonical LF record 后的 cursor 有效。

### 5.1 event ID 全局唯一的有界证明

event ID 的 identity body 包含 canonical event type、timestamp、source sequence、source ref 与经济 body；每种 event type
只允许出现在唯一固定 stream。v1 采用以下精确边界：

1. 同一 stream 内重复 event ID 会同时重复其 source key，由严格递增检查失败；
2. 不为 1,000 万事件维护无界全局 set；跨流或跨 timestamp 的相同 SHA-256 被视为 SHA-256 collision trust assumption，必须在
   source evidence 中显式登记，不能表述为信息论意义上的全局唯一证明。

若未来风险审查要求检测任意跨 timestamp digest collision，必须发布新 resource policy，并采用受控 external-sort/spill
实现，不能静默增加无界内存。

### 5.2 continuity / gap 的精确定义

| stream | v1 可重算的 `gap_count=0` 含义 |
| --- | --- |
| contract | 与 snapshot catalog 第 2..N 项逐条 exact 对应：activation ts、四元 refs、source event ID；无缺失/多余 transition |
| bar | `bar_end_ts == event.ts`，15 分钟窗口连续、不重叠；评价期从第一个完整 bar 到 `end_ts` 形成固定 lattice |
| funding | 按各 snapshot segment 的 sealed cadence/anchor/cap/floor 生成 expected settlement lattice 并逐条 exact 对应 |
| index / mark | 只证明 raw stream 内严格顺序、身份与声明 coverage；event-driven v1 不声称上游在没有记录时仍“无缺口” |
| tradable | 只证明 source-sealed observation 顺序与 singleton；不声称市场每个时刻均可交易或容量完整 |

因此 index/mark/tradable 的 zero gap 是“没有可由当前 schema 推导出的 cadence gap”，不是市场数据完整性证明；真实数据
authority 和 promotion 继续保持 NO-GO。

### 5.3 Snapshot catalog

catalog 第 1 项是 warmup carry-in，不对应 phase 05 event。其后每项必须与 contract stream 相同 ordinal 的 event exact 对应。
对每个 `[activation_i, activation_{i+1})` segment，formal preflight 用 segment 自身 refs 调用 snapshot loader；最后 segment
覆盖至 `end_ts`。不得要求 carry-in refs 覆盖整个 run，也不得把未来 transition 提前加载为当前 economic state。

## 6. 第二遍、恢复与提交边界

第二遍启动前重新读取并验证 manifest，然后重新打开六流。恢复 cursor 必须同时满足：

1. cursor schema、stream fingerprint、offset/count/resource bound 合法；
2. 从文件头流式重算至 `next_byte_offset`，prefix raw/semantic digest、count、last key exact；
3. offset 恰好位于 LF record boundary；不得 seek 后从任意字节直接信任后缀；
4. 继续读取时每条 event 仍执行与第一遍相同的 schema、identity、ordering、lineage 与 continuity 校验；
5. drain 完成后的 cursor 与 stream ref 完全一致；六流与 manifest 的第二遍最终 identity 与第一遍完全一致。

merge/engine 可以读取 look-ahead record，但 checkpoint 只能在完整 timestamp group 的全部 source event、derived barrier、
decision/order 与 ledger 已 flush/fsync 后，复制该 timestamp 的 committed cursors。reader cursor 与 committed cursor 必须使用不同
字段/对象；禁止把“已从 descriptor 读取”误报为“经济状态已提交”。

2026-08-29 对抗复审进一步证明：即使第一遍已通过，攻击者仍可在第二遍中途原位改写文件并恢复 size/mtime，使早期 record
先被 yield、尾部校验后才失败。因此当前 `DerivativesEventVerificationPassV1` 只允许做身份验证，公开属性固定
`economic_mutation_allowed=false`。在接 reducer 前必须二选一并形成新验收证据：

1. 推荐：使用调用方显式提供、容量预检通过的私有 staging root，将第二遍验证字节写入不可寻址/独占临时 spool；全部六流、
   funding lattice 和 manifest 完成后，只从已封闭 spool 产生 committed records；或
2. reducer 全部状态、ledger 和 cursor 位于可丢弃事务中，只有六流 drain 与最终 identity PASS 后一次性提交，并以故障注入证明
   任意 late failure 的外部经济状态、checkpoint 和 publisher 均为零变化。

该边界未实现前，A2a 不得标记完成，verification read cursor 也不得进入正式 checkpoint。

## 7. A2b 独立 decision-input-set

feature 不新增为第七种 market event；新增独立 `derivatives-decision-input-set/v1`，由未来 semantic request 与 event-set
并列绑定。manifest 固定包含：

- feature stream ref：canonical relative path、size、raw SHA-256、record count、semantic digest、coverage、source registry ID、
  parent raw hashes、first/last bar key；
- exact parameter snapshot ref：path、size、raw/semantic SHA-256、Step 2/3 round ID、combo key、legacy parameter fingerprint、
  Decimal parameter fingerprint、decision/feature policy ID/version/fingerprint；
- feature resource policy：1 MiB/record、512 MiB/file、最多与 bar stream count 相等；
- `authority_status=synthetic_test_only` 与 `capital_promotion_eligible=false`。

feature stream 使用与 event source 相同的 stable path、canonical JSONL、双遍和 cursor 规则，并与 bar stream锁步。每个会产生
decision fact 的 bar 必须恰有一条 feature；缺失、重复、顺序漂移、未被 bar 引用的 feature 均失败。

## 8. Sealed feature 与 Decimal parameter schema

`BarFeatureRecordV1` 必须至少绑定：

- 固定 scope：`BTC-USDT-SWAP`、`independent_15m`；
- `bar_start_ts`、`bar_end_ts` 与由 bar window、OHLCV、bar source ref 计算的无循环 observation fingerprint；
- long/short 两侧的 `alpha`、`momentum`、`trend`、`microstructure`、`confidence` 标准化 Decimal 分量；
- 仅用于策略观察的 funding rate、observed timestamp 与 source ref；它绝不能产生 settlement cash flow；
- feature transform policy ID/version/fingerprint 与完整 source record identity。

所有数值使用 canonical Decimal string；禁止 JSON number、float、bool-as-int、默认值和 extra field。consumer 每次从 raw line
重解析、重算 source record hash，并 exact 匹配 `BarCloseEventV1.feature_ref`。

`IndependentParameterSnapshotV1` 必须绑定父 candidate raw path/size/SHA、Step 2/3 IDs、combo key 与 legacy typed fingerprint；
正式数值另以 canonical Decimal strings 建立 `decimal_parameter_fingerprint`。秒数/计数只能是 exact int，布尔只能是 JSON bool。
fee、MMR、liquidation fee、tick、lot 或 tier 不得由 parameter 覆盖，必须来自当时 active snapshot。

legacy JSON number 的首次转换必须从原始十进制词法完成并封存；禁止先经 binary float 再转 Decimal。

## 9. Decision state 与待实名批准的业务决策

`IndependentDecisionStateV1` 只保存策略可恢复状态：parameter/feature/algorithm fingerprint、processed bar count、last bar end、
last feature hash、last decision ID，以及有固定上限的 canonical Decimal score history。position、cash、holding age、cooldown anchor 与
live queued order 属于 reducer economic view，不得在 decision state 复制成第二真源。

纯 API 固定为输入 `bar + loaded feature + loaded parameter + prior decision state + reducer economic view + active snapshots +
warmup/evaluation phase`，输出 `decision fact + optional signed target-position intent + next state`。warmup 推进 state，但不得创建
order 或计入 metrics；已有 live IOC 时仍生成 fact/推进 state，但 intent 为 null 且 reason 固定为 `blocked_by_live_order`。

以下两项属于资金含义，现有代码和文档没有权威答案，**不得由实现者自行选择**：

1. **评分/信号公式**：五类 long/short component 如何加权、确认窗口、entry/exit/tie、short-disable、min-hold、cooldown、
   drawdown/catastrophic gates 的精确 Decimal 公式和版本；
2. **仓位规模**：推荐 parameter snapshot 强制提供正的 `target_position_contracts`，再按 active instrument lot/min/tier 验证；
   备选是版本化固定 sizing policy。禁止沿用 legacy adapter 的硬编码 1 张。

在实名 RDP Owner 与 Independent Risk Reviewer 对两项共同签字前，只能实现 schema/loader/state 校验和“不得产单”的测试桩；
不得实现或声称 `derivatives-independent-decision/v1` 已完成。

## 10. 错误与状态模型

- 输入合同、身份、资源、稳定读取和 continuity 失败均是 run-level blocked，不降级为 no-signal；
- 错误输出只包含稳定 code、非敏感 artifact identity 和 stream kind，不含文件正文、绝对路径、凭证或环境变量；
- 两遍漂移统一 `event_set_identity_changed`；cursor/prefix 不符统一 `event_stream_cursor_mismatch`；具体内部原因记录在受控诊断，
  但不得扩大公共错误面；
- 被 blocked 的运行只能进入独立 diagnostic publisher，不能出现 result/metrics 或 completed manifest。

## 11. 验收矩阵

### 11.1 A2a formal source

1. manifest/stream root escape、case drift、symlink、junction/reparse、读取中替换和两遍间替换全部失败；
2. BOM、非法 UTF-8、重复 key、NaN/Infinity、CRLF、空行、无 final LF、超 1 MiB line 全部失败；
3. raw size/hash、count/digest、first/last、coverage、lineage 多报/少报、kind/stream 漂移全部失败；
4. order/singleton、catalog transition、bar lattice、funding lattice 全量重算通过；
5. cursor 行中/UTF-8 中、错误 prefix、错误 stream、look-ahead 冒充 committed、resume 后身份漂移全部失败；
6. 相同 event-set 的第一遍、第二遍和 checkpoint resume 得到逐字节相同 cursor 与 semantic identity；
7. 聚焦测试、衍生品 replay 回归、Windows 全量 unit、独立 code review 无未关闭 P0/P1。

### 11.2 A2b decision input

1. feature 缺失/重复/未引用、bar mismatch、parent/record hash、raw/canonical identity 漂移全部失败；
2. parameter unknown/missing/float/bool-as-int、父 candidate/round/combo/fingerprint 漂移全部失败；
3. `dataclasses.replace()` 篡改 loaded feature/parameter/state 后消费失败；
4. 不同 ambient Decimal context、chunk size、zero-run、checkpoint resume 得到逐字节相同 fact/intent/state；
5. warmup 零 order/metrics，live IOC 不覆盖，fee 只来自 active snapshot；
6. 第 9 节两项实名批准已形成可审计记录，并进入 algorithm/policy fingerprint；
7. 独立风险和数据血缘复审无未关闭 P0/P1。

## 12. 实施顺序与回滚

1. 先实现 A2a formal source 与 fault tests；不接 reducer；
2. 实现 A2b decision-input manifest、feature/parameter strict loader 和不可产单 state contract；
3. 取得第 9 节实名决策后，实现纯 Decimal decision transition 与 golden vectors；
4. A2a/A2b 双重验收后才开始 LF-B1.2-B reducer；
5. 回滚只移除未接消费者的 internal modules/tests；不可变测试 artifact 不原位覆写。

本文不替代上位 ADR 的正式批准，也不改变 Research OS G0、真实数据充分性、live 或资本晋级门禁。

## 13. 2026-08-29 实施与复审记录

- 已实现并保留：manifest parse 前 4 MiB gate、exact-case/无链接路径、单描述符有界 JSONL、canonical record、
  raw/semantic/count/boundary/lineage exact 重算、bar lattice、catalog transition、snapshot-aware funding、prefix cursor 与两遍
  verification；非空 stream 的 source registry/parent lineage 多报或少报均失败，空流两组 lineage 必须为空；
- 第二轮独立复审发现 `finish()` 未封闭已 drain stream 与已加载 snapshot 的第二遍最终身份；现已在返回任何完成
  evidence 前重新复核 manifest、event/snapshot root、全部 stream/snapshot file identity，并重新加载比较 snapshot bytes；
- 资源修正：funding 改为增量 event validator，不再缓存最多 1,000,000 个 funding object；snapshot raw bytes 按 immutable ref
  intern，unique snapshot materialization 固定上限 512 MiB；
- 独立复审未接受“verification pass 可直接执行经济 mutation”的原实现，因此 API 已更名并硬编码
  `economic_mutation_allowed=false`；transactional spool/reducer rollback 仍为 P1；
- A2b 初版代码经对抗审查发现六项 P1：未锚定 formal event-set/bar stream、未读取父 candidate、第二遍尾部 TOCTOU、deny-list
  可绕过、没有 restartable feature reader、decision state 未绑定 active identity。该初版未保留，当前仍只有本文设计，未产生订单；
- 本地验证：Ruff `aats/ --fix` 通过；衍生品回放目录 `258 passed, 1 skipped`；Windows 全量 unit
  `6553 passed, 32 skipped, 259 subtests passed`。相关唯一 skip 是 Windows 无法替换仍打开的 descriptor；两项最终
  identity/空流 lineage 修复经独立只读复审未发现新增或遗留 P0/P1；
- 仍待补：positive funding/catalog transition、完整 cursor/resource/fault matrix、POSIX descriptor replacement、transaction boundary，
  以及实名批准的 parameter allow-list、评分公式和仓位规模；因此 LF-B1.2-A2 整体继续 OPEN。
