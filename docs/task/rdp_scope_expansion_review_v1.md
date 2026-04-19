# Phase 1-4 详细设计 · 第一次审查

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 审查视角:可落地性、内部一致性、现有代码兼容性、blast radius、测试充分性

## R1-01 ❌ Scope 唯一约束有逻辑缺陷

**现状设计**(§1.1):

```sql
DROP INDEX IF EXISTS governance.uq_active_combo;
CREATE UNIQUE INDEX uq_active_scope ON governance.active_parameter_sets(scope, scope_ref, family, timeframe)
    WHERE scope = 'combo';
```

**问题**:
1. 既有 `uq_active_combo` 是 `(family, timeframe)` unique CONSTRAINT(不是 INDEX)——`DROP INDEX` 根本删不掉
2. 现有 `aats/data_platform/governance/active_parameter_sets_db.py` 的 UPSERT 逻辑依赖这个约束名字
3. drop 旧约束 + 加 partial unique index 会**破坏**既有 upsert 的 `ON CONFLICT` 目标

**影响**:Phase 1 migration 会让既有的 combo-level apply 写入全部失败

## R1-02 ❌ `family='_profile'` 占位符是 hack

**现状设计**(§1.3):"`family='_profile'`(占位,便于既有 combo 查询不误抓)"

**问题**:
1. `family` 有 NOT NULL + 已有 CHECK 约束(见 batch_a_04 的约束列表)
2. 注入 `_profile` 这种特殊值会污染既有按 family 聚合的 query(尤其 `aats/api/rdp_control_summary.py` 里所有 `by_combo` 逻辑)
3. 将来 `profile` scope 如果需要多维(比如 `profile × symbol`)则无法扩展

**建议**:将 `family` 对 profile scope 改为 NULLABLE;用 `scope` 字段区分而不是 family hack

## R1-03 ❌ `timeframe` 对 profile scope 无意义但 NOT NULL

同 R1-02,profile scope 下 `timeframe` 字段冗余。不解决就得塞 `_profile` 或 `*`。

## R1-04 ⚠ clamp 的 single source of truth 不明

**现状设计**(§8):"`strategy_profile_seed.py` 的 clamp 范围与 Profile research job 使用的 clamp 来源一致"——但没说**怎么保证一致**。

**问题**:
- clamp 硬编码在 `strategy_profile_seed.py` 里(多个 profile 的 `_clamp_float` 调用)
- Profile research job 需要读 clamp 来圈 grid,现在没有提取接口
- 若 seed 改了 clamp 但研究代码没跟上,静默产出违规 rec

**建议**:提取 `get_profile_clamps(profile_id) -> dict[str, tuple[float, float]]` 作为 single source;seed + research 都调这个

## R1-05 ⚠ Grid size 200 points × 多 profile 的性能

**现状设计**(§1.2):200 grid points × N profile × 90 day replay

**问题**:
- 现有 `gold.market_swap_replay_bars_*` 每个 timeframe 几万条,计算 Sharpe / MaxDD / 活跃度全 grid 每次运行 ≈ 数分钟至小时
- workflow timeout = 1800s,在 N_profiles ≥ 3 时可能超时
- research 是 weekly,真正慢的不是 CPU 而是 I/O(每次全量读数据)

**建议**:
- grid 先降到 27(3×3×3)跑通,后续再加密
- 或改用 coordinate descent(先只优化一个维度到稳定,再下一个)

## R1-06 ❌ Shadow 期 feature flag 放哪里没说清

**现状设计**(§1.7):"数据库上引入 `feature_flag` 表项"

**问题**:
- 项目里没有统一的 feature_flag 表
- 需要新增一张表还是用 env var?env var 会被 CI 镜像层泄露(批次 A 硬化刚解决的问题)
- flag 写 DB 则需要 API 端点去 flip,又多一个 attack surface

**建议**:用 `governance.system_config`(如不存在则新建)kv 表,flip 走 API 需 operator token;明确写入设计

## R1-07 ❌ Phase 1 apply 路径没交代 apply_token

**现状设计**(§1.5):"POST /rdp/profile-recommendations/{id}/apply"

**问题**:既有 `aats/api/rdp_apply_token.py` 只认 `action ∈ {apply, rollback, freeze}`。profile apply 也要带 token 吗?用哪个 action?新增 action `profile_apply`?

**建议**:复用 `action=apply`,通过 recommendation 的 scope 字段在服务端校验是否需要额外人工签名;action 不增加

## R1-08 ⚠ profile_type_review streak 的原子性

**现状设计**(§1.1):`profile_type_review_streak` 表 `streak_count` 简单自增

**问题**:如果 research job 并发(手动触发 + schedule 重合),自增会竞争

**建议**:streak 自增走 `UPDATE ... SET streak_count = streak_count + 1 WHERE last_run_id != :new_run_id` 原子 CAS;或用 advisory lock

## R1-09 ❌ Phase 2 cost calibration 读 DB 跨库

**现状设计**(§2.2):`calibrate_cost_from_fills(session, ...)` 读 `execution_fills`

**问题**:
- `execution_fills` 表在 `aats_live_derivatives`(live DB)
- `cost_calibration_runs` 在 `aats_research`(research DB)
- RDP 进程只连 research DB(RDP_DATABASE_URL),访问不到 live DB 的 fills

**建议**:
- A. RDP daemon 也开 live DB 连接(read-only)
- B. live 侧做 ETL 把 fills 镜像到 research.bronze,research 读 bronze

B 更干净但工作量大。**本 SOW 选 A**,read-only 连接,env var `AATS_LIVE_DB_URL_RO`

## R1-10 ⚠ Sleeve advice 需要 live DB 聚合

同 R1-09:`strategy_sleeve_intents` + realized edge 数据都在 live DB

## R1-11 ❌ 回滚路径缺 release_id 记录

**现状设计**(§1.8):"走既有 parameter_apply_history 表审计"

**问题**:既有 `parameter_apply_history` 没有 `scope` 列。profile apply 时写入会和 combo apply 混在一起,rollback 查 combo `(family, timeframe)` 的逻辑抓不到 profile 记录。

**建议**:Phase 1 migration 把 `parameter_apply_history` 也加 `scope + scope_ref` 列

## R1-12 ❌ VALID_REC_TYPES 变更会影响既有测试

**现状设计**(§1.1):`VALID_REC_TYPES` 加三个新值

**问题**:
- 有多处测试 hard-code 了 `sorted(VALID_REC_TYPES)` 的结果(比如错误消息里的列表)
- `recommendations_db.py:62` 的 `f"合法值: {sorted(VALID_REC_TYPES)}"` 会变,既有测试若断言这段文本会挂

**建议**:加入新值的 commit 要全局 grep `parameter_upgrade|keep_active` 看断言,一并修

## R1-13 ⚠ UI 改动缺 Hero 顶带整合

**现状设计**(§1.6):新区块独立

**问题**:
- 既有 Hero 顶带显示"待审批 N / 观察中 M / 阻断 K / 队列 L"
- profile_upgrade 应该进 Hero 哪个数字?
- profile_type_review 应该进哪个数字?

**建议**:
- profile_upgrade(status=draft)→ 并入"待审批"(但 UI 的点击路径要能区分 combo vs profile)
- profile_type_review → 独立新 Hero 字段"待人工审查 P"(profile_type_review)

## R1-14 ⚠ 缺部署期 migration 顺序

**现状设计**(§7):"合并 Phase 1 schema migration"

**问题**:没说 migration 怎么跑。项目有 `run_migrations()` 但 batch_a 走的是 Python runner + SQL 文件混合,新 batch_b 跟这个模式还是另起?

**建议**:明确 batch_b 沿用 batch_a 的 Python + SQL 混合模式;`aats/data_platform/migrations/_batch_b.py` 管 Python 侧

## R1-15 ⚠ cost calibration 的 slippage 计算方向模糊

**现状设计**(§2.2):"sum((fill_price - decision_price) × sign)"

**问题**:什么是 `decision_price`?
- 下单时的 mid?best bid/ask?limit offset 后的 price?
- 现有 `execution_orders` 表有 `expected_price`?还是 `submitted_price`?
- 没明确选哪个字段,实现时容易偏差

**建议**:使用 `execution_orders.expected_fill_price`(如有)或 submitted 时的 mid;明确字段名

## R1-16 ⚠ Profile research 没说产出多少 rec

**现状设计**(§1.2):每个 profile 产 0-1 条 profile_upgrade rec

**问题**:
- 如果本周最佳 candidate 和 current 完全一样(数值都在 clamp 内但 Sharpe/MaxDD 差不多)→ 产 keep_active?还是什么都不产?
- 对比 combo 流程,research 每轮**必然产出至少一条** keep_active,UI 上 operator 能看到"研究完成"

**建议**:profile research 也应每轮必产 1 条——即使没改动,产 `keep_active` 类型的 profile rec,UI 显示"本周研究完成,未建议调整"

## R1-17 ❌ Phase 3 advice 和 recommendation 关系矛盾

**现状设计**(§3.1):`sleeve_budget_advice` 表 `recommendation_id` 可选 + §0.2 `sleeve_budget_adjust` 是 recommendation_type

**问题**:
- advice 到底是不是 recommendation?两张表并存会导致混乱
- observation-only 的"advice"如果不进 `recommendations` 表,就游离于治理体系之外

**建议**:二选一
- A. 只用 `recommendations` 表,`scope='sleeve'` + `recommendation_type='sleeve_budget_adjust'`,advice 表删除
- B. 只用独立 `sleeve_budget_advice` 表,不碰 recommendations,但 UI 和 API 自成体系

**选 A**:统一治理体系,UI / API / observability 复用

## R1-18 ⚠ observability 缺 RDP daemon 本身的健康告警

**现状设计**(§6):"release_cycle task failure rate > 10%"

**问题**:Phase 0 已证明 daemon 的单任务失败能隐藏 8h 才被发现。光告警 release_cycle 不够

**建议**:
- 增加"any workflow failure rate > 20%(1h rolling)"
- 增加"daemon heartbeat 停止 > 10 分钟"
- 增加"rdp_task_queue pending > 5 超过 10 分钟"

## R1-19 ⚠ 缺 migration 的 dry-run / reversibility 约束

**现状设计**(§1.1):DDL 直接写

**问题**:batch_a 有 rollback 脚本(`batch_a_99_rollback.sql`)但没说 batch_b 也要有

**建议**:batch_b 每个 01/02/03/04 都配一个 99 rollback;部署 SOP 明确"先 rollback 测通再正向"

## R1-20 ❌ Phase 1 与 live DB `strategy_profile_activation` 同步缺

**现状设计**(§1.1):只动 research DB

**问题**:
- 追因报告已明确:`aats_live_derivatives.strategy_profile_activation` 是实盘读参数的真正源头
- Phase 1 只改 research DB 的 active_parameter_sets(scope=profile)是不够的
- 必须在 apply 阶段**同步写** live DB 的 `strategy_profile_activation.payload`

**建议**:
- `POST /rdp/profile-recommendations/{id}/apply` 必须跨库写:
  1. research DB 的 `active_parameter_sets` 更新
  2. research DB 的 `parameter_apply_history` 记录
  3. live DB 的 `strategy_profile_activation.payload` 更新,actor 从 `system_seed` 变 `rdp_apply`
  4. live DB 的 `strategy_profile_activation_history` 记录
- 四步跨库,需要 saga 或两阶段提交;或串行 + 明确失败补偿

---

## 审查结论

- **5 个 Blocker**(❌):R1-01, R1-02, R1-03, R1-06, R1-07, R1-09, R1-11, R1-12, R1-17, R1-20
  (数数下来其实 **10 个 blocker**,修正。)
- **9 个 Warning**(⚠):其余

**建议所有 Blocker 必须在 Step 3 优化时解决,Warning 至少解决 6 个。**
