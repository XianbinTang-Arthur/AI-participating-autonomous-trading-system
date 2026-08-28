# RDP 衍生品回测 LF-B1.2 事件与证据内核实施任务书

> 文档状态：现行实施任务书；LF-B1.2-A1 与 A2 event-set 严格契约基础已完成本地静态验收，A2 formal reader/decision input 及 LF-B1.2 整体仍执行中、未验收
> 最后核对：2026-08-28（起始基线 `main@0fb27c0a152c`；以本文档所在 HEAD 为准）
> 上位设计：[`../design/rdp_derivatives_backtest_run_v1_adr_2026_08_28.md`](../design/rdp_derivatives_backtest_run_v1_adr_2026_08_28.md)
> 上位任务：[`rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md`](rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md)
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 问题与当前行为

LF-B1.1 已提供固定 `BTC-USDT-SWAP` linear、USDT、isolated、single-position 作用域的严格
合同和纯 Decimal 记账，但它刻意没有 I/O、事件源、状态 reducer、publisher、checkpoint、恢复、CLI、
数据库或工作流接入。当前调用方仍可自由构造算术 facade；系统也没有证明 instrument、tier、fee 和
funding schedule 来自哪份不可变历史证据，不能据此生成资本晋级证据。

本切片建设 LF-B1.1 与后续 promotion metrics/qualification round 之间的可信内核边界。所有输入必须先
由 byte identity、source identity、effective window 和严格 schema 共同证明；任何缺口只产生稳定的
blocked/diagnostic 原因，绝不补值、猜测、排序修正、回退 latest 或发布“成功”产物。

## 2. 目标与非目标

### 2.1 目标

1. 定义 instrument、tier、execution fee、funding schedule 四类不可变 snapshot ref 和稳定、有界 loader；
2. 在任何经济状态变化前完成 exact digest/size、canonical bytes、source registry identity、作用域与
   `[start_ts,end_ts)` effective-window preflight；
3. 定义封闭的 derivatives event union、逐流严格顺序、全局唯一 event ID 和固定 phase merge；
4. 实现 source-sealed mark/index freshness、真实 funding settlement cadence/cap/floor 与 continuity 门禁；
5. 实现 isolated single-position reducer，固定同时间 phase、`q(t-)` funding、下一可交易事件成交、事务式
   margin rejection、两次 liquidation check 和不可回看的 bar-close decision；
6. 实现 event-set 两遍 identity 验证、exact checkpoint/recovery、manifest-last immutable publisher 与
   completed/diagnostic schema 隔离；
7. 以 golden、fault injection、determinism、recovery、resource cap 和独立复审证明边界。

### 2.2 非目标

- 不接入 CLI、Gateway、RDP UI、数据库 writer、Phase 6 或 live runtime；
- 不支持 ETH、inverse、dated futures、options、cross margin、hedge mode、多仓位或多 live queued order；
- 不把 bar close、last trade 或 Gold 对齐字段伪造成 mark/index/funding settlement；
- 不从目录扫描、latest 指针、环境变量或 request 自报字段推断父证据；
- 不在本切片产生 `capital_promotion_eligible=true`；v1 永久为 false；
- 不因后续 UI 重构隐藏 queued/blocked/failed 状态或放宽任何门禁。

## 3. 事实源、输入和输出

### 3.1 输入事实源

- 已封存的 instrument contract snapshot 原始 bytes；
- 已封存的 tier、execution fee、funding schedule snapshot 原始 bytes；
- `derivatives-event-set/v1` manifest 及其逐流不可变 JSONL；
- exact Step 2/3 candidate lineage、opening account state、window 和固定 policy IDs；
- 已有稳定文件 reader、strict JSON decoder、typed JSON identity 与 LF-B1.1 算术合同。

路径只用于定位，不能构成身份。所有身份判断基于稳定读取的原始 bytes、lowercase SHA-256、精确 byte size、
规范 schema、canonical content identity 和显式 source registry UUID。

### 3.2 正式输出

成功或强平运行固定发布 12 个非 manifest 文件及最后一个 `manifest.json`；blocked 运行只能发布到独立
diagnostic 根，且不得包含收益、promotion metrics 或可被 qualification consumer 接受的 child schema。
正式 artifact set、result semantic 和 semantic run fingerprint 必须保持无环依赖。

## 4. 设计约束

### 4.1 Snapshot 边界

- ref 必须严格拒绝未知/缺失 key、非 canonical UUID/SHA-256、非正 size、绝对/逃逸/符号链接路径、
  naive 或非 UTC 时间、空或越界 effective window；
- loader 使用单描述符稳定读取并在读取前后复核文件与父目录身份；读取后 exact 校验 size/hash，严格解析
  UTF-8、重复 key、非有限值和顶层类型，并要求 payload bytes 已是 canonical JSON；
- instrument payload 必须通过 `InstrumentContractSnapshot.from_dict()`，再由已验证对象派生
  `LinearPerpetualContractV1`；禁止从 request 自由字段构造历史合同；
- tier/fee/funding schedule payload 必须逐字段映射到 LF-B1.1 合同，禁止默认值与自由覆盖；ref、payload 和
  request 的 snapshot ID、schema、source registry、scope、effective window 必须 typed-exact 一致；
- 四个 snapshot 必须共同覆盖完整评价窗口，切换时必须由 phase 05 原子替换完整四元组。
- Python 进程内的 `Loaded*` 与 freshness cursor 只是可重算的 value object，不是权限 token；
  不使用可导入或可被 `dataclasses.replace()` 携带的私有 sentinel 充当安全边界。
  loader/value object 在每次构造时重验 raw/ref/派生经济参数；真正的 source authority 边界是后续
  event-set preflight 对已封存 bytes 的验证，内核不接受外部自报的派生 state 作为证据。

### 4.2 事件和时间

- source stream 按 `(ts,source_sequence)` 严格递增；不得静默排序或去重；event ID 是对不含自身的 canonical
  event payload 计算的 lowercase SHA-256，因此重复内容具有同一身份，跨流内容具有可验证的全局身份，
  避免为 1,000 万事件维护无界去重集合；
- `source_sequence` 的 wire 范围固定为 `0..2^63-1`；更大整数在任何 event ID 计算前以稳定错误失败；
- 全局键固定为 `(ts,phase_priority,source_sequence,event_id)`，phase 为 `05/10/20/30/40/50/55/60`；
- 时间 wire 为 UTC RFC3339 `Z` 微秒格式，评价窗口为 `[start_ts,end_ts)`，`ts == end_ts` 不进入本窗口；
- bar decision 只能在 bar close 后创建至多一个 queued order，且只能由严格晚于 decision timestamp 的第一条
  scope/source/freshness 合法 tradable event 以 taker IOC 语义一次性解析为 full/partial/no-fill，不得跳过后
  选择更有利事件；
- funding 使用 phase 30 前仓位 `q(t-)`；同 timestamp fill 不能回写该 funding cash flow；
- mark/index 在 funding、liquidation、fill、bar close 与 end valuation 时都必须存在且 age `<=60s`；
- funding settlement 必须与当时 schedule 的 cadence、cap/floor、observed time 和 exact timestamp 一致。
- event JSONL 的 raw artifact digest 保留 locator 以证明确切字节；semantic event digest 只由
  `event_id`/排除 locator 的 identity body 聚合。manifest 必须同时绑定两者，不得让目录搬迁
  改变 semantic run identity，也不得因语义相同而忽略 raw bytes 漂移。
- 每流 integrity summary 必须绑定固定 policy/version/fingerprint、检查 coverage、event count/digest 和
  gap/duplicate/order/singleton-cardinality 零失败结果；它进入 semantic identity，但正式 reader 仍须从 raw
  event 重算，不得把 summary 或 `passed=true` 当作 authority token。singleton kind 固定为
  contract/funding/tradable/bar 并进入 policy fingerprint；mark/index 只要求同 timestamp 内 sequence 严格递增。
- snapshot catalog 第 1 项是 warmup carry-in，不产生 phase 05；其余项与 contract stream 一一对应。
  manifest 先关闭 count 与 first/last activation，第一遍 reader 再逐条核对 timestamp、四元 refs 和 event ID；
  catalog 内不同 immutable snapshot 不得复用同一大小写无关 locator。

### 4.3 状态、发布和恢复

- reducer 只持有单一 isolated net position、free cash、isolated balance、当前四元 snapshot、mark/index cursor
  和最多一个 queued order；
- 增加风险的 fill 先在临时状态计算 fee/PnL/IM/tier，失败时不改变任何经济状态或 ledger；
- phase 40/55 均按原始 Decimal 不等式检查 liquidation；触发后取消 queued order、记录 forced close 并终止；
- phase 40/55 由 merge 后的固定 engine policy 确定性派生，不是输入流；同 timestamp 最多一条 tradable，
  先完成 funding 再插 phase 40，tradable 后插 phase 55；
- 含 opening leg 的反转订单作为整体 provisional transaction；任一 opening IM/tier 校验失败则 closing leg 也不
  提交。纯 reduce-only close 仍不因 IM 拒绝；
- checkpoint 必须绑定 semantic request、event-set、四元 snapshot、engine/policy IDs、每条流 exact cursor、
  reducer state、ledger prefix count/digest 和 checkpoint 自身 digest；恢复只接受调用方提供的 exact ref；
- publisher 只写同父私有 staging，逐文件 flush/fsync，最后写 manifest、fsync 目录并原子 rename 到从未存在的
  final 目录；相同 identity 仅允许 exact byte retry，漂移为 conflict；
- consumer 只接受 final 目录、exact 文件集合、`complete=true`、manifest-last 和全量 hash/schema 复核。

## 5. 实施顺序

1. **LF-B1.2-A：snapshot、event 与 formal-source 边界**
   - snapshot refs、payload schemas、稳定 loader、effective-window preflight；
   - event dataclasses/strict parser、固定 phase、逐流验证、deterministic merge、freshness cursor；
   - canonical JSON/JSONL、metadata-preserving cursor、event-set 两遍 identity 与 resource limit helpers；
   - exact feature record/parameter snapshot 输入及显式 checkpointable Decimal decision state；不得复用 mutable/
     float `IndependentReplayAdapter`，不得接受调用方自报 order command。
2. **LF-B1.2-B：reducer 与 golden path**
   - opening state、schedule activation、mark/index、funding、queued order、fill、position 和 liquidation；
   - decision adapter 只能消费 bar-close fact 并返回固定、纯、可重放命令；
   - 生成全部 ledger 的内存 canonical records。
3. **LF-B1.2-C：publisher 与 recovery**
   - 运行中 event-set identity 重算及 committed timestamp cursor；
   - immutable child/diagnostic publisher、manifest validator、exact checkpoint/recovery；
   - crash point、文件替换、并发 conflict、prefix drift 与恢复确定性测试。
4. **LF-B1.2-D：独立复审与收口**
   - 金融正确性、安全、并发、TOCTOU、资源、时间语义和可维护性复审；
   - 从模块级到 Windows 全量、WSL2 最窄集成（若本切片引入 PostgreSQL）验证；
   - 更新上位 SOW、ADR 实施状态和任务索引；范围清晰后才创建本地 commit。

## 6. 测试矩阵

至少覆盖：

- snapshot hash/size/source/schema/window/symbol 漂移、non-canonical bytes、重复 key、symlink、读取中替换；
- observed-forward snapshot 不能回填历史，四元 snapshot 缺一项或不同步切换失败；
- 每类合法事件 round-trip；未知 key/type、float 经济字段、非 UTC、非 canonical time/ID、非法 sequence 失败；
- 流内乱序、重复 event ID、同 key 冲突、不同输入容器顺序下全局 merge 结果一致；
- mark/index missing/stale、future cursor、funding 缺失/重复/错 cadence/越 cap-floor/迟到失败；
- funding 与同 timestamp fill 的 `q(t-)`、phase 40/55 两次强平、bar-close 无回看、end-exclusive；
- margin rejection 零状态突变，反向成交拆腿 fee typed-exact，free cash 不救 isolated liquidation；
- 两次相同运行所有 canonical ledger/result/semantic fingerprint 一致；
- checkpoint 前缀篡改、请求/event-set/policy 漂移、latest fallback、partial staging、manifest 提前出现、
  final overwrite、并发同 ID 漂移全部失败关闭；
- 单记录、单文件、event count、queued order、checkpoint 和 artifact-set 资源上限。

## 7. 安全、兼容和回滚

- 新包保持 internal，默认无入口、无后台任务、无网络、无数据库写入、无 live 副作用；
- 不修改现有 SPOT `backtest-run/v2`、legacy Step 2 或 Phase 6 公共 API；
- 失败 reason 只含稳定 code 与非敏感 identity，不包含凭证、环境内容或任意文件正文；
- 回滚方式是在未接入消费者前回退 LF-B1.2 模块与测试；已发布 immutable 测试产物不原位修改，按审计保留
  或由明确的测试清理流程移除；
- 任一 P0/P1 未关闭、测试失败、真实数据证据不足或 ADR 未批准时，保持 producer/qualification/UI apply 路径关闭。

## 8. 验收条件

1. 所有可进入经济状态的字段均可追溯到 hash-bound snapshot/event bytes；消费边界只从已重验
   raw/ref 派生状态，对 value object 使用 `dataclasses.replace()` 篡改 bytes、经济合同或 event body 必须失败；
2. phase、freshness、funding、fill、margin、liquidation 和 end valuation 的 golden ledger 可独立重算；
3. 两遍 event-set、publisher、checkpoint 和 recovery 的 fault/determinism 测试通过；
4. 完成文件集合与 diagnostic 集合严格隔离，任一不完整/漂移输入不会产生 completed result；
5. Ruff、模块单测、相关共享回归、全量 unit 与必要集成通过；两名独立只读审查者无未关闭 P0/P1；
6. 上位文档准确记录静态、运行时和 UNKNOWN 边界；不宣称已接入完整 RDP、已产生可晋级证据或可上线；
7. UI/UX 全面重构仅在本内核状态与 API 稳定后启动，并必须把执行状态、数据完整性、研究可信度、晋级资格、
   真人审批和 live 门禁分层展示，不得以视觉成功状态覆盖事实缺口。

## 9. 当前状态

- 已完成：LF-B1.1 严格合同与纯 Decimal 算术；LF-B1.2 现状审查和本任务书；LF-B1.2-A1 的
  immutable snapshot refs/loader、四元 effective-window、封闭事件联合类型、严格逐流/全局 merge、phase 40/55
  内部 barrier、mark/index freshness 与 funding continuity/settlement 验证；
- LF-B1.2-A2 event-set 契约基础已完成：strict manifest/ref、六流 raw/semantic identity、hash-bound 且必须重算的
  integrity summary、warmup carry-in catalog/phase-05 一致性、immutable snapshot locator、评价期 bar、固定资源上限、
  restart cursor、4 MiB component/final gate 与 manifest raw path/size/SHA/canonical-byte 校验；该结果仍不包含文件 I/O
  双遍 reader、真实 raw event 预检或运行时经济状态；
- LF-B1.2-A1 本地静态验证：Ruff 通过；本目录聚焦测试 `204 passed`；连同共享 instrument arithmetic/snapshot
  回归 `255 passed`；Windows 全量 unit `6128 passed, 31 skipped, 259 subtests passed`。两名独立只读审查者均
  给出 PASS，未发现未关闭 P0/P1；未访问网络、数据库、live profile 或真实资金；
- LF-B1.2-A2 契约基础本地静态验证：Ruff `aats/ --fix` 通过；衍生品回放目录 `236 passed`；Windows 全量 unit
  `6160 passed, 31 skipped, 259 subtests passed`。两轮对抗复审逐项修复后最终无未关闭 P0/P1；未执行文件
  reader、网络、数据库、WSL2、部署、live profile 或真实资金；
- 执行中：LF-B1.2-A2 metadata-preserving 双遍 formal reader 与 sealed feature/Decimal decision input；完成后才进入 LF-B1.2-B reducer/golden ledger，
  再进入 LF-B1.2-C publisher/checkpoint/recovery；
- 未完成：canonical JSONL event-set 有界文件读取/两遍原始身份重算、feature/decision input、single-position reducer、正式 publisher/validator、exact
  checkpoint/recovery、正式数据运行接入和 UI/UX 全面重构；因此本任务书整体仍未验收；
- 本轮已关闭的审查项：公开 `event_order_key()` 全事件重验；event/funding 共用 strict snapshot transition；
  integrity policy 明确并绑定 singleton kind 集；manifest ref 不再忽略 raw bytes；catalog/phase-05、评价期 bar、
  `(ts,source_sequence)` boundary、wire array、casefold path、4 MiB aggregate/final gate 均失败关闭；
- 已登记的非阻断 P2：snapshot loader 的 symlink/junction/读取中替换/非法 UTF-8/重复 key/超限等 wrapper 级
  fault-injection 补测、自定义 iterator 原生异常归一化，以及 funding expected lattice 的增量化；这些不得在最终
  LF-B1.2 验收时遗留为未处置风险；
- reducer 开工前已冻结的 P1 设计修正：validated activation/decision step、feature/parameter 可重算输入、exact
  lot-floor/price、经济子状态原子性与 IOC lifecycle 分离、open/queued snapshot switch 语义、timestamp-boundary
  checkpoint，以及 formal publish 的 POSIX/WSL durability 边界；实现与故障测试仍未完成；
- 外部边界：真实历史数据充分性、Research OS G0、真人 Owner/Reviewer/许可/预算、live 与资本晋级均未因本任务改变。
