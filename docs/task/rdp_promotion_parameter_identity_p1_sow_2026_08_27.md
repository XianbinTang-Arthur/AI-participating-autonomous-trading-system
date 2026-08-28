# RDP 参数晋级内容身份绑定 P1 任务书

> 文档状态：现行实施任务书；LF-A 工程验收候选，LF-B 数据库触发器仍待真人审批
>
> 最后核对：2026-08-28（起始基线 `main@c15ccd2d5057`）
>
> 核对范围：当前代码差异、单元与隔离 PostgreSQL；不证明部署、现场参数或生产安全
>
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 目标

关闭参数晋级链路中的内容替换窗口：一条 Phase 6 资格证据不能只凭
`parameter_set_id` 证明参数；系统必须把候选参数值的确定性指纹带入资格结论，并在 dry-run
及真正 apply 的锁内数据库行上重新计算、逐字节语义比较。与此同时，受支持的参数集和
recommendation 写入口必须采用“身份只写一次、生命周期单独推进”的语义，禁止同一 ID 通过
UPSERT 被改绑到另一组 values、来源轮次或业务身份。

自动导入旧候选时，生命周期写入还必须携带读取时的期望状态；若并发 apply 已把候选推进为
`released`，导入流程只能报告 CAS 冲突并跳过，绝不能把已发布参数覆盖成 `deprecated`。

本任务只修复治理与资本控制平面的应用层事实，不触发 RDP、不应用参数、不部署 live profile、
不读取凭证，也不把应用层约束误报为数据库原生不可变。

## 2. 整改前行为与根因

1. `governance.parameter_sets` 的官方 UPSERT 在同一 `parameter_set_id` 冲突时会覆盖
   family、symbol、timeframe、source round、dataset version、values 和 confidence。
2. Phase 6 `parameter_upgrade_candidates.json` 只保存参数集 ID 和评分结论，不保存参数值或
   参数值指纹。
3. promotion qualification 只比较 ID、family/timeframe/symbol 和 source round；因此旧 values
   通过资格后，同 ID 的新 values 仍可能被读取。
4. apply 虽然会锁住参数集行并与事务外 registry 快照比较，但二者都可能来自已经被改写的同一
   DB 行，无法证明 values 与 Phase 6 证据一致。
5. recommendation 的官方 UPSERT 同样允许同一 recommendation ID 改绑目标、来源和证据引用，
   与已有的状态 CAS 身份绑定纪律不一致。
6. 自动导入根据事务外 registry 快照找到旧 `candidate` 后，原状态更新只按 ID 写入；若 apply
   抢先将其晋级为 `released`，旧导入仍会异步覆盖成 `deprecated`。同时应用层合法状态与数据库
   约束不一致，审计会把 `released` 误报为非法。

## 3. 设计

### 3.1 参数值指纹

- 新增版本化 `aats.parameter_values.v1` 指纹。
- 输入严格为 JSON object；递归拒绝非字符串 key、非 JSON 类型和 NaN/Infinity。
- 使用 UTF-8、键排序、无空白 canonical JSON，并在 payload 中加入固定 schema domain；输出为
  64 位小写 SHA-256。
- Phase 6 候选必须携带 `parameter_values_fingerprint`。旧候选没有该字段时不得继续晋级，必须
  重新运行当前代码的决策轮。

### 3.2 资格与授权

- promotion qualification 验证候选指纹格式，并把它写入合格 verdict。
- promotion guard 对合格 verdict 强制要求该指纹，并由进程内短时 authorization 原样携带。
- recommendation 继续通过 `target_parameter_set_id + source_round_id + evidence_bundle_ref` 指向
  精确候选；本切片不新增数据库列。

### 3.3 Apply 双重核验

- 任何 dry-run 或 apply 在返回成功前，都必须把 registry 中的 values 重算后与资格 verdict 比较。
- 真正 apply 在 combo advisory lock 和 parameter row lock 内再次基于 canonical DB values 重算；
  不匹配返回稳定错误且零资本写入。
- 锁后业务写入只消费已通过上述比较的 canonical DB values。

### 3.4 官方写入口只写一次

- parameter set UPSERT 只允许完全相同的不可变身份重试；冲突重试不得改写 status 或生命周期
  字段，不同身份返回稳定冲突。
- recommendation UPSERT 只允许完全相同的业务内容重试；审批、驳回、替代等生命周期必须继续
  使用现有 CAS transition。
- 新 recommendation 必须先以 `draft` 插入；同 family/symbol/timeframe/type 的旧 draft 替代
  与新 draft 插入共用 scope advisory transaction lock，并在一个事务中提交。新内容冲突或写入
  失败时旧 draft 不得先在内存/文件中变成 superseded；事务提交后 registry 必须以 DB 完整回读
  替换本地快照，包含并发 writer 已提交的其他 scope 记录。
- 受管 DB 写失败或身份冲突不得降级为仅写 JSON；仅 parameter registry 与明确标注支持的离线
  读取/导入路径在未配置 DB 的开发模式保留文件兼容。recommendation 新增与生命周期写入要求
  canonical DB，不承诺离线文件 writer。
- parameter set 生命周期写入使用 expected-status CAS；自动导入只有 CAS 成功才计入废弃数量。
- `released` 必须同时存在于数据库约束、应用白名单和迁移审计白名单；它只能由受控 apply
  生命周期产生，自动导入仍只允许 `draft/candidate` 初始状态。
- Step 3 参数集 ID 由版本化 round/combo/content identity 确定性生成；并发 importer 对同一内容
  得到同一 ID，由 insert-once writer 线性化，不生成同轮同 combo 重复候选。
- 同轮完整性按期望 combo 与 immutable content 全集合核对，不能以“任意一行存在”代替完成；
  中断后只补缺失 combo，内容冲突失败关闭。全部新候选 insert/verify 完成后才 CAS 废弃旧候选。
- 受管 DB 在状态 CAS 冲突后必须重读 canonical registry 再写 JSON 审计镜像，避免把旧
  `candidate` 重新写回镜像。
- parameter/recommendation 的通用 lifecycle helper 不得产生 `released`；参数 released 仅由受控
  apply/rollback 资本事务拥有。recommendation 只允许 draft→approved/rejected/superseded 及
  approved→superseded，终态不能回退。

### 3.5 Step 3 发布与自动导入边界

- Step 3 candidate 使用 `aats.step3_candidates.v1`，必须显式携带 round、scope symbol、
  dataset version 与 `candidates` object；禁止把缺失 `candidates` 的顶层对象当候选解析。
- `aats.step3_round.v1` manifest 必须最后、不可变发布，并绑定 phase、整体状态、UTC 时间、
  scope、dataset version，以及 candidate 的相对文件名、字节数和 SHA-256。目录、candidate、
  manifest 任一身份不一致均零写入失败关闭。
- managed governance DB 环境还必须在运行级成功 marker 之前插入一次不可变 `phase2_step3`
  research snapshot，逐字锚定 candidate/manifest UTF-8 bytes、digest/size、精确 Step 2 DB snapshot
  identity 与父 candidate digest。同 round ID 仅允许 exact retry；DB 缺失/冲突/不可达时保留磁盘审计
  产物但不输出成功 marker、不导入、不进入 Phase 3/4。禁止 file lazy bootstrap 获得受管信任。
- 正式 round ID 只接受 `{YYYYMMDD}_{HHMMSS}_{uuid8}`。同秒 round 依据已绑定 manifest 的
  `started_at` 排序；最新未完成或无效 round 不得静默回退到旧候选。
- 受管 DB（以及实际可连接的 governance DB）导入使用全局 session-level advisory lock，锁覆盖
  artifact 选择、DB 真源重读、insert/CAS 和镜像刷新；锁忙返回可重试的 `import_lock_busy`，
  不视为成功。
- 自动 supersession 仅限 `succeeded` round 以 `candidate` 导入，且旧项必须为同
  `family + symbol + timeframe`、同 Step 3 来源，并有可信 manifest 证明 incoming
  `started_at` 严格更新。`partial_success` 只能导入为 `draft`；旧证据缺失或时间倒退返回
  `supersession_deferred` 并保留旧候选。
- Step 2 candidate/manifest 使用 `aats.step2_candidates.v1` / `aats.step2_round.v1`，绑定正式
  round、symbol、dataset、非空 window、完整 combo 声明和 candidate digest/size。显式目录无效、
  最新组无效或最新 `started_at` 并列均失败关闭；Step 3 `succeeded` 必须引用并复核该份
  `succeeded` Step 2 的 digest、window 与数据身份。
- Step 3 `succeeded` 必须完成全部预期 calibration、merge、四个非空 combo、零 pending、零
  constraint violation/auto-fix；跳过或部分完成只能发布 `partial_success`。受管 DB 导入先逐项
  staging 为 draft，再在同一事务整体公开为 candidate；精确重试允许合法生命周期已经推进但
  绝不回退。自动 supersession 只由 canonical status 仍为 candidate 的新成员驱动，replacement
  状态核验与旧候选 CAS 共用 apply combo lock。
- Step 2 的三项 calibration 与四项 formal scan、Step 3 的两项 expanded calibration 都按
  expected tuple 校验精确长度、非空 key、唯一性、exact set 和逐项 succeeded；重复/错名结果
  不能靠数量凑成 `succeeded`。Step 3 baseline consumer 与自动 importer 必须再次复核 manifest
  topology，不能只信 producer 自报的整体 status。
- Step 2/Step 3 分别发布唯一运行级 JSON marker，绑定本子进程产生的 round、绝对路径、digest、
  status、scope 与 window；Step 3 marker 额外绑定实际 Step 2 round/digest。full pipeline 对长任务
  实时转发 stdout/stderr，只保留唯一 marker，不缓存完整日志；缺失/重复 marker、路径越界、
  digest 漂移、Step 3 managed snapshot 发布失败或跨 pipeline Step 2 身份不一致均在进入下一阶段前
  失败关闭。
- full pipeline 执行研究阶段前必须已有非空 `--start/--end`；阶段执行后不得再以全局 latest 扫描
  替换本轮 Step 2/Step 3 产物。自动导入支持精确 candidate 路径并验证其正式目录结构；仅在显式
  resume 且本次未执行 Step 3 时允许沿用最新可信 round 选择逻辑。
- apply 在取得 combo、recommendation、parameter row lock 且准备首个资本写入之前，再按锁内
  canonical recommendation 和原 capability 重跑完整 promotion qualification；等待锁期间资格
  或授权过期返回 `promotion_qualification_changed_at_lock_in`，active/history/release 均零写入。
- Phase 3/4 one-shot 子进程分别发布 immutable result sidecar 与唯一 marker，绑定调用 scope、resolved
  参数指纹以及父进程会消费的全部关键输出 path/digest/size。父进程按
  `round/per_combo/<combo>/result_<uuid>.json` 精确读取，只解析已验字节，禁止使用执行前后目录差或
  latest 推断 run；同 combo 并发和污染目录不能交叉绑定。父进程通过描述符读取并在读取前后核对
  文件身份，拒绝符号链接、路径替换或读取期间变化。

### 3.6 正式 Step 3 lineage 单一验证真源

- 业务目标：Phase 3/4 与 promotion 只能消费当前项目根内、可由正式 importer 完整接受的
  `succeeded` Step 3 candidate；形似正式目录的自洽孤立文件不得取得治理身份。
- 模块职责与接口：`auto_import_candidates` 提供唯一的正式 candidate 加载/验证结果，统一返回精确
  path、原始 bytes、SHA-256、payload 和 manifest metadata；importer、parameter lineage 与
  promotion qualification 均消费该结果，禁止复制 topology、时间或 Step 2 链校验。
- 数据与数据库身份：复用 `governance.research_round_snapshots`，以新 phase `phase2_step3` 保存
  candidate/manifest 原文、digest/size、payload 与 Step 2 snapshot fingerprint。Promotion 以该受管根
  对照磁盘 artifact，再物化 importer 会产生的 deterministic parameter-set identity，并核对目标 ID、
  family、symbol、timeframe、source round、`source_phase=step3_merged` 与 values fingerprint；Phase 3/4
  candidate SHA 必须逐字匹配该 artifact。
- 一致性、并发与生命周期：正式加载使用稳定文件读取，并将同一 candidate/manifest bytes 与 exact DB
  snapshot 对照后完成 digest、payload 和 lineage 绑定；candidate+manifest+内部哈希的同步重写仍失败；
  qualification 只读，不改变 draft/candidate/frozen/released 生命周期。Apply 仍须在现有 combo/row lock
  内重跑 qualification 并核对 DB canonical values，因此 artifact 或 DB 身份漂移均在资本写入前关闭。
- 授权与安全：本项不放宽认证、审批、capability 或 live 门禁；仓库外路径、symlink、缺失 Step 2、
  子产物 digest/size 漂移、未来/错序时间和非完整 topology 均失败关闭，不记录凭证或连接字符串。
- 错误与幂等：合法 artifact 重复验证结果确定；无法证明正式 lineage 时使用稳定的
  `promotion_candidate_step3_lineage_invalid`，不回退 latest、不写 DB/文件、不应用参数。
- 缓存与性能：不新增缓存；每次 qualification 按精确 source round 读取小型 manifest/candidate 与其
  已声明子产物。正确性优先于跳过验证；后续若缓存，必须以 immutable digest 为键并保持同等失败语义。
- 日志、监控与审计：现有 qualification verdict/reason code 是审计面；本项不把静态测试写成运行健康。
- 测试：覆盖仓库外 formal path、Step 2 child digest 漂移、Phase 3/4 SHA 与正式 artifact 不一致、目标
  parameter-set ID/values 与 importer 身份不一致，以及合法完整链的成功路径。
- 迁移、回滚与兼容：旧 flat/Step 2 参数文件继续是 `unbound` 诊断输入；旧 Phase 3/4/Phase 6 证据若
  缺少可证明的正式 Step 3 链必须重跑，不做伪回填。代码回滚不得恢复宽松晋级。
- 配置与环境隔离：默认项目根取当前安装代码根；测试或受控调用可显式传入 project root。不得从候选
  自身目录反推并信任任意仓库根。
- 部署与验收：本切片只做代码、Ruff 和聚焦测试，不提交、不部署、不触发 RDP/live；通过不等于
  production-ready。

### 3.7 Phase 3/4 已绑定产物的业务语义复核

- Phase 3 父流程严格校验 attribution CSV 完整表头、scope、显式 UTC 时间窗、唯一事件身份、
  alignment 状态、正式 taxonomy/reason code，以及 aligned 行所需 live lineage；随后从已验 CSV
  重新计算 `attribution_summary.json` 与 `top_failure_modes.json` 并逐值比较。
- Phase 4 父流程严格校验 execution alignment、fill feasibility、slippage 三份 CSV 的完整表头、
  scope、候选身份、UTC 时间对齐、OHLC/区间/notional 数学关系和有限数值。父流程复用正式
  feasibility、slippage、execution-cost 模型从 alignment 重算两层明细与 cost summary，并逐值核对
  source run、symbol、timeframe 和 window 身份。
- 空研究窗口只能使用正式空产物及其可重算空摘要；畸形表头、非法枚举、缺失 lineage、NaN/Infinity、
  伪造聚合、摘要与明细不一致均失败关闭。该校验只证明产物内部一致与代码模型可重现，不证明策略
  盈利、市场数据充分或 live 可用。

## 4. 范围

### 包含

- 参数值 canonical fingerprint helper；
- candidate、qualification、authorization、dry-run/apply 锁内绑定；
- parameter set/recommendation 官方 DB writer 的 insert-once 语义；
- 文件 registry 的重复 ID 防护；
- 自动导入与 apply 的 candidate→deprecated/released CAS 竞态防护；
- 单元、负向、并发语义与真实 PostgreSQL 兼容验证；
- 当前文档、reason code 和验收证据同步。

### 不包含

- Stage 20 schema、trigger、DELETE 防护或数据库权限收紧；这些属于现行任务书已登记、未经真人
  批准不得实施的 LF-B。
- 历史 recommendation 或旧 Phase 6 产物的伪回填；旧产物必须重新计算。
- live 参数应用、真实订单、真实资金、G1 解锁、push。

## 5. 稳定失败语义

- `parameter_values_invalid`
- `promotion_candidate_values_fingerprint_invalid`
- `parameter_set_evidence_fingerprint_mismatch`
- `parameter_set_immutable_identity_conflict`
- `recommendation_immutable_identity_conflict`
- `decision_round_snapshot_immutable_identity_conflict`
- `round_metadata_invalid`
- `round_content_conflict`
- `import_lock_busy`
- `supersession_deferred`

错误日志不得包含参数之外的连接信息、凭证或数据库驱动原文。

## 6. 验收标准

1. 同一参数集 ID 以不同 values/source/identity 重试时，官方 DB writer 拒绝且原行不变。
2. 同一 recommendation ID 以不同目标/来源/证据/理由重试时拒绝且审批状态不回退。
3. Phase 6 新候选始终包含可复算的 64 位指纹；旧候选或畸形指纹资格失败关闭。
4. 资格指纹与事务外 registry 不一致时 dry-run 失败；资格后、锁前参数发生变化时锁内 apply 失败，
   且 active/history/release/recommendation 生命周期均无资本写入。
5. 相同身份的幂等重试成功，但不借 UPSERT 改写生命周期。
6. 自动导入读取到 `candidate` 后若并发 apply 已推进到 `released`，replacement/旧候选检查受同
   combo lock 串行化，导入不回退新状态、不移走 fallback、不增加废弃计数；同 artifact 精确重试
   返回 canonical released/frozen/deprecated 状态，迁移分布检查接受合法 `released`。
7. 部分导入重跑只补缺失 combo；同轮内容漂移或重复 combo 失败关闭；新候选全部存在前不废弃旧候选。
   跨 symbol、draft/partial round、旧轮回放、manifest/hash 漂移均不能移走当前候选；真实
   PostgreSQL 下并发 importer 只能有一个进入临界区。
8. 相关 Ruff、unit、最窄 PostgreSQL 集成、`git diff --check` 通过；独立 code review 无未关闭 P0/P1。
9. 文档明确：在 LF-B trigger 获批并落地前，具备直接 SQL/superuser 权限的越权更新仍是未关闭
   风险，因此不得宣告数据库原生不可变或生产就绪。
10. 两条并发 full pipeline 不会把 B 的全局最新 Step 2/Step 3 candidate 拼接到 A；长阶段日志实时
    可见且 marker 只允许出现一次。畸形 `pending_validation`、非标准 round 目录和未经绑定的显式
    candidate 路径均零写入失败关闭。
11. 两条并发 Phase 3/4 child 调用各自只消费唯一 sidecar 指向的 run；污染目录、marker/sidecar
    不一致、越界/符号链接、scope/参数指纹不符、输出 digest/size 漂移及畸形 JSON/CSV 均失败关闭。
12. Formal Step 3 lineage 与 importer 共用同一完整验证入口；promotion 必须把 Phase 3/4 candidate
    SHA 与目标 deterministic parameter-set identity 回查到该 artifact，缺失或任一身份不一致均失败关闭。
13. Phase 3/4 父流程必须从已绑定明细重算归因、feasibility、slippage 与 cost summary；缺失 lineage、
    非有限数值、业务字段错配或明细/汇总不一致不得进入 round 聚合。

## 7. 回滚

本切片不改 schema。代码回滚会恢复旧 writer/qualification 行为，因此回滚后必须继续保持
REAL-MONEY NO-GO；不得用回滚绕过指纹缺失或身份冲突。已经生成的带指纹 Phase 6 JSON 为向后
兼容的附加字段，可保留审计。

## 8. 实施顺序

1. 先锁定 canonical fingerprint 与旧候选失败关闭测试。
2. 将指纹贯穿 candidate、verdict、authorization 和 apply 双重核验。
3. 将 parameter set/recommendation writer 改为身份只写一次，并补 registry split-brain 测试。
4. 运行定向到全量验证和独立复审；修复发现后再提交单一范围本地 commit。
5. 另行等待真人审批 LF-B Stage 20；不得在本任务中越权实施 trigger。

## 9. 2026-08-28 本地验收证据

- Windows Ruff：`aats/` 全量通过；相关 scripts/tests 定向 Ruff 通过。
- Windows unit：`5791 passed, 31 skipped, 259 subtests passed`；skip 与既有可选环境门一致。
- Promotion/Step 3 identity、approve/release、control plane 与 Phase 3/4 child 聚焦回归：`237 passed`。
- Phase 3/4 child 严格 sidecar 与业务语义专测：`13 passed`；覆盖并发同 combo、污染目录、digest
  漂移、缺 lineage、聚合不一致、畸形 JSON/CSV、非法 UTF-8 与非有限数值。
- Step 2/Step 3 artifact contract、full-pipeline contract 与独立 Step 3 脚本：`59 passed, 1 skipped`；
  skip 为既有可选环境门。
- WSL2 隔离 PostgreSQL：promotion identity、原子 candidate publication/retry/supersession、
  recommendation insert/supersede/rollback/concurrency 共 `17 passed`。
- 上述均是本地代码与隔离数据库证据，不证明部署、当前 RDP campaign、现场治理库、真实参数或
  生产安全；本轮没有触发 RDP、没有 apply recommendation、没有交易或 live 副作用。
