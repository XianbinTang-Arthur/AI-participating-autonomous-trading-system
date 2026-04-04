# RDP 与主交易系统整合方案任务书（正式版）

## 1. 目标

将现有 **Research Data Platform (RDP)** 从“独立可运行的研究/归因/治理/决策平台”，整合为主交易系统的正式旁路子系统，使其能够：

1. 稳定读取主交易系统产生的事实数据
2. 对这些事实数据进行研究、归因、执行可行性分析与治理
3. 将参数候选、运营建议、上线 readiness 以**受控方式**回灌主交易系统
4. 为 operator / strategy owner / reviewer 提供统一的观察、排障和决策支持入口

本整合方案的目标**不是**让 RDP 直接参与实时下单，也**不是**让 Phase 6 自动修改生产参数。  
第一版整合的目标是：

> **让 RDP 成为主交易系统的正式 research / attribution / governance / decision support 子系统。**

---

## 2. 整合原则

### 2.1 不侵入实时主链
主交易系统的实时主链仍保持：

```text
Market -> Feature -> Decision -> Governance -> Strategy -> Allocator -> Execution -> Reconciliation -> Operator
```

RDP 不插入这条链路中的每次实时决策，不阻塞实时交易，不成为实时链的同步依赖。

### 2.2 旁路分析 + 受控回灌
RDP 的定位是：

- 离线 / 准实时研究
- replay / attribution / execution realism
- 参数治理
- 决策建议输出
- 受控回灌到生产配置

### 2.3 研究与生产隔离
继续保持：

- **production DB**
- **research DB (`aats_research`)**

分离，禁止一开始合库。

### 2.4 建议生成与参数应用分离
Phase 6 输出 recommendation，不直接生效。  
参数应用必须经过：

```text
research result -> governance -> approval -> apply
```

而不是：

```text
research result -> auto apply
```

---

## 3. 当前系统关系（整合前）

当前主交易系统已经具备：

- 实时行情接入
- 特征计算
- baseline / AI 决策
- policy / risk / health
- strategy family / sleeve / allocator
- execution / reconciliation / recovery
- operator 查询与控制面

当前 RDP 已具备：

- Phase 1：数据底座
- Phase 2：参数研究
- Phase 3：live attribution
- Phase 4：execution proxy realism
- Phase 5：artifact / parameter / round 治理
- Phase 6：闭环决策建议

但两者当前仍主要是“共存”，而不是正式整合。

---

## 4. 整合后的目标架构

整合后的高层架构应为：

```text
                           +---------------------------+
                           |   主交易系统 (Realtime)    |
                           |---------------------------|
                           | market_gateway            |
                           | feature_engine            |
                           | decision_engine           |
                           | governance_engine         |
                           | strategy_engines          |
                           | execution_engine          |
                           | reconciliation_service    |
                           | operator                  |
                           +-------------+-------------+
                                         |
                                         | 事实数据 / 只读接口 / 配置回灌
                                         v
                     +---------------------------------------------+
                     | RDP (Research / Attribution / Governance)   |
                     |---------------------------------------------|
                     | Phase 1: data foundation                    |
                     | Phase 2: research                           |
                     | Phase 3: attribution                        |
                     | Phase 4: execution realism                  |
                     | Phase 5: governance                         |
                     | Phase 6: decision support                   |
                     +-------------------+-------------------------+
                                         |
                                         | approved parameter set / decision suggestion
                                         v
                         +--------------------------------------+
                         | Production Config / Active Parameters |
                         +--------------------------------------+
```

---

## 5. 哪些部分会实际用到 RDP

整合后，主交易系统中至少有 4 个区域会实际使用 RDP 的产出。

### 5.1 策略参数管理
RDP Phase 2 / 5 / 6 将影响：

- `independent` family 参数
- `directional` family 参数
- `15m` / `1H` family-timeframe 级默认值
- active / frozen parameter set 的版本管理

### 5.2 Operator 排障
RDP Phase 3 的 attribution 输出将直接服务：

- “为什么没下单”
- “为什么 replay 认为应该开而 live 没开”
- “问题卡在哪一层”

### 5.3 执行假设与风险策略
RDP Phase 4 将为以下问题提供依据：

- 当前成本假设是否过于乐观
- 哪些机会 execution realism 很差
- 哪些 family / timeframe 更适合保守运行

### 5.4 运营治理与上线决策
RDP Phase 5 / 6 将提供：

- 参数 freeze / candidate / deprecated 生命周期
- family/timeframe keep_active / lower_priority / pause / require_review 建议
- promotion readiness 评估

---

## 6. 整合范围

### 6.1 第一版必须包含

1. **事实数据对接**
   - RDP 可稳定读取 production live 事实数据
2. **参数回灌机制**
   - 主系统可加载 active parameter set
3. **Operator 可见性**
   - operator 可看到 RDP 的核心结论
4. **受控应用流程**
   - recommendation -> approval -> apply

### 6.2 第一版不包含

1. RDP 直接参与实时链上的每笔决策
2. Phase 6 自动修改生产参数
3. Phase 6 自动 pause / restart 实盘 family
4. research DB 与 production DB 合库
5. orderbook / trades 级 execution realism 深度整合

---

## 7. 整合任务分为 4 个阶段

## 阶段 A：事实数据与接口对接

### A.1 目标
让 RDP 能稳定读取主交易系统的事实数据，而不是依赖临时约定或人工指定。

### A.2 需要确认的事实来源

#### 主交易系统数据库表（只读）
RDP Phase 3 当前依赖：

- `strategy_sleeve_intents`
- `portfolio_allocation_decisions`
- `allocator_budget_snapshots`
- `reconciliation_state_snapshots`
- `strategy_execution_bundles`
- `execution_orders`
- `execution_fills`

第一阶段需要正式确认这些表的：

- 数据库位置
- 访问方式
- 只读权限
- 字段契约
- 历史保留周期
- 索引与查询性能

### A.3 需要新增或固化的内容

#### 1. RDP 专用 live 只读连接配置
建议在 RDP 配置中新增：

- `RDP_LIVE_DATABASE_URL`
- `RDP_LIVE_DB_READONLY=true`

#### 2. live schema contract 文档
新增文档：

```text
docs/operations/live_schema_contract_for_rdp.md
```

内容包括：
- 表名
- 核心字段
- 时间字段
- 主键 / 关联键
- 允许的 null / 状态值

#### 3. live query adapter 收口
将 Phase 3 中对 live DB 的读取进一步抽成统一 adapter 层，避免散落脚本里直接 SQL。

### A.4 交付物

- `docs/operations/live_schema_contract_for_rdp.md`
- RDP 读取 live DB 的统一配置
- 至少 1 个验证脚本，检查 live DB 连接与关键表可读

### A.5 验收标准

1. RDP 在不改主交易系统逻辑的前提下，能读取 live facts
2. Phase 3 one-shot 能在标准环境中稳定运行
3. 字段契约文档齐全

---

## 阶段 B：参数回灌机制

### B.1 目标
让 Phase 2 / 5 / 6 的结论，能以**受控方式**影响主交易系统的实际运行参数。

### B.2 核心思想
不要让主交易系统直接理解所有 Phase 产物。  
主交易系统只需要理解一个概念：

> **当前 active parameter set 是什么**

### B.3 建议新增对象

#### 方案一（推荐）：文件型 active parameter set
新增目录：

```text
configs/active_parameter_sets/
  independent_15m.json
  independent_1h.json
  directional_15m.json
  directional_1h.json
```

或使用单一文件：

```text
configs/active_parameter_sets/active_parameter_registry.json
```

#### 方案二：数据库型 active parameter registry
后续可做，但第一版不建议。

### B.4 需要新增的主系统能力

#### 1. active parameter loader
建议新增：

```text
aats/bootstrap/active_parameters.py
```

职责：
- 启动时读取 active parameter set
- 解析 family/timeframe 维度的参数
- 合并到 strategy profile / default settings 上

#### 2. strategy parameter injection
需要在以下区域接入 active parameters：

- `independent` family 参数载入
- `directional` family 参数载入
- `15m / 1H` family-timeframe 级别覆盖

#### 3. 参数优先级规则
建议明确：

```text
hardcoded defaults
  < strategy_profiles/*.yaml
  < active parameter set
  < runtime emergency override（如果有）
```

### B.5 需要新增的应用脚本

建议新增：

```text
scripts/apply_active_parameter_set.py
```

功能：
- 从 `current_parameter_registry.json` / `recommendation_registry.json`
- 选择 approved / frozen 的 parameter set
- 写入 `configs/active_parameter_sets/...`

### B.6 交付物

- `aats/bootstrap/active_parameters.py`
- `configs/active_parameter_sets/`
- `scripts/apply_active_parameter_set.py`
- `docs/operations/active_parameter_application.md`

### B.7 验收标准

1. 主系统启动时能加载 active parameter set
2. family/timeframe 参数能被 active set 覆盖
3. 参数应用是显式动作，不是自动隐式更新

---

## 阶段 C：Operator / 观察面整合

### C.1 目标
让 operator 不需要手工去 artifacts 目录翻文件，也能看见 RDP 的关键结论。

### C.2 第一版要暴露的内容

#### 1. 当前 active parameter sets
展示：
- family
- timeframe
- active parameter set id
- values
- frozen / candidate 状态
- source round

#### 2. 最近一次 attribution 结论
展示：
- top failure modes
- latest attribution round
- combo 状态

#### 3. 最近一次 execution realism 结论
展示：
- full_fill_ratio
- total_execution_cost_mean
- positive_adjusted_edge_ratio

#### 4. 当前 family/timeframe 决策状态
展示：
- keep_active / lower_priority / pause / require_review
- latest recommendation id
- readiness

### C.3 整合位置
建议新增只读接口到：

- `aats/services/operator`
- API gateway
- UI summary panel

### C.4 建议新增 API

例如：

- `GET /rdp/parameters/active`
- `GET /rdp/attribution/latest`
- `GET /rdp/execution/latest`
- `GET /rdp/decisions/latest`
- `GET /rdp/recommendations/latest`

### C.5 建议新增 UI 模块

- Active Parameter Sets 卡片
- Latest Attribution 卡片
- Latest Execution Realism 卡片
- Family/Timeframe Decisions 表格

### C.6 交付物

- operator 侧只读 API
- 对应 UI summary 页面 / section
- `docs/operations/operator_rdp_integration.md`

### C.7 验收标准

1. operator 不进入 artifact 目录也能看到关键信息
2. 当前 active parameter set 可视化
3. 最近决策与 readiness 可视化

---

## 阶段 D：受控应用流程

### D.1 目标
建立 recommendation -> approval -> apply 的完整生产整合流程。

### D.2 流程定义

```text
Phase 2 / 3 / 4 / 5 / 6
  -> generate recommendation
  -> reviewer/operator approve
  -> apply active parameter set
  -> production restart / reload（若需要）
  -> observe
```

### D.3 必须区分的状态

#### recommendation status
- `draft`
- `approved`
- `rejected`
- `superseded`

#### active parameter status
- `inactive`
- `active`
- `stale`
- `deprecated`

### D.4 第一版不做自动 apply
必须保留人工审批动作。

### D.5 可选新增脚本

```text
scripts/approve_recommendation_and_apply.py
```

功能：
1. 审批 recommendation
2. 生成 active parameter set
3. 记录操作日志
4. 可选输出后续重启/ reload 指令

### D.6 交付物

- recommendation approval 规则
- parameter application SOP
- `docs/operations/recommendation_to_production_workflow.md`

### D.7 验收标准

1. 从 recommendation 到 active parameter set 的流程可执行
2. 应用行为可追踪
3. 无自动黑盒改生产参数

---

## 8. 主交易系统需要改的具体位置

## 8.1 Bootstrap / 配置层

### 需要改的模块
- `aats/bootstrap/settings.py`
- 新增 `aats/bootstrap/active_parameters.py`

### 要做的事
1. 新增 active parameter set 读取入口
2. 定义参数覆盖优先级
3. 在系统启动时把 active parameters 注入 family 配置

## 8.2 Strategy Family 配置层

### 需要改的区域
- `independent` family 参数构造
- `directional` family 参数构造

### 要做的事
1. 支持 family/timeframe 级别的参数覆盖
2. 与现有 `strategy_profiles/*.yaml` 合并
3. 明确参数名与 RDP 参数名映射关系

## 8.3 Operator Service / API Gateway

### 需要改的区域
- `aats/services/operator`
- API gateway 路由
- UI 展示层

### 要做的事
1. 增加 RDP 只读查询接口
2. 显示 active parameter sets
3. 显示最近 attribution / execution / decisions
4. 后续支持 recommendation approval（可选）

## 8.4 部署与环境配置

### 需要改的内容
1. 新增 `.env.research` 与 live readonly DB 配置
2. 新增 artifacts 根目录配置
3. 新增 active parameter set 文件路径配置

---

## 9. 新增建议文件/目录

建议新增以下内容：

```text
aats/bootstrap/active_parameters.py
configs/active_parameter_sets/
docs/operations/live_schema_contract_for_rdp.md
docs/operations/active_parameter_application.md
docs/operations/operator_rdp_integration.md
docs/operations/recommendation_to_production_workflow.md
scripts/apply_active_parameter_set.py
scripts/approve_recommendation_and_apply.py   # 可选
```

---

## 10. 不建议的整合方式

### 10.1 不要把 RDP 插入实时每笔下单链
不要让主系统在每次开仓前去同步调用：

- replay
- attribution
- execution realism
- decision round

### 10.2 不要让 Phase 6 自动修改生产系统
Phase 6 只做建议，不直接生效。

### 10.3 不要一开始合并 research DB 与 production DB
继续保持隔离。

### 10.4 不要让主系统直接读取所有 artifacts 目录
主系统最好只读取：
- active parameter set
- decision summary / registry
而不是直接理解全部研究产物。

---

## 11. 推荐实施顺序

### 第 1 步：打通事实数据
- 确认 live DB 表契约
- 固定 RDP 的 live readonly 连接

### 第 2 步：做 active parameter set
- 新增 loader
- 主系统 family 参数注入

### 第 3 步：做 operator 可见性
- 加只读 API
- 加 UI summary

### 第 4 步：做 approval -> apply 流程
- recommendation approval
- parameter application

---

## 12. 最小整合范围（MVP）

第一版整合只要求：

1. RDP 能稳定读取 production facts
2. 主系统能读取 active parameter set
3. operator 能看到当前 active parameter 与最近 decision
4. recommendation 能经过人工批准后应用

这就足以形成：
- 研究结果可回灌
- operator 可观测
- 系统可受控演进

---

## 13. 验收标准

整合方案通过条件：

1. RDP 可读取主系统 live 只读数据
2. 主系统可加载 active parameter set
3. family/timeframe 参数可由 active set 覆盖
4. operator 可查看：
   - active parameter sets
   - latest attribution
   - latest execution realism
   - latest family/timeframe decisions
5. recommendation -> approval -> apply 流程可执行
6. 无自动黑盒改生产参数
7. research DB 与 production DB 仍保持隔离

---

## 14. 一句话总结

本整合方案的职责是：

> **把 RDP 从独立研究平台，整合为主交易系统的正式旁路子系统：它稳定读取生产事实数据，产出研究/归因/执行可行性/治理/决策建议，并通过 active parameter set 与 operator 观察面，以受控方式回灌主交易系统。**
