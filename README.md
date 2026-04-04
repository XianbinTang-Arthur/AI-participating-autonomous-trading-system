# AIParticipatingAutonomousTradingSystem

> 面向加密资产交易的事件驱动、可审计、可恢复、受保护的自动交易系统原型。

---

## 目录

- [1. 项目概览](#1-项目概览)
- [2. 当前能力边界](#2-当前能力边界)
- [3. 核心设计原则](#3-核心设计原则)
- [4. 系统架构总览](#4-系统架构总览)
- [5. 关键主链路](#5-关键主链路)
- [6. 主要模块说明](#6-主要模块说明)
- [7. 策略与自动执行控制](#7-策略与自动执行控制)
- [8. 执行层、恢复层与退出聚合](#8-执行层恢复层与退出聚合)
- [9. 运行配置模型](#9-运行配置模型)
- [10. 支持的托管 Profile](#10-支持的托管-profile)
- [11. 快速开始](#11-快速开始)
- [12. PostgreSQL 与迁移](#12-postgresql-与迁移)
- [13. 启动 API / UI](#13-启动-api--ui)
- [14. 常见运行模式](#14-常见运行模式)
- [15. 重要配置项说明](#15-重要配置项说明)
- [16. Operator 认证与权限模型](#16-operator-认证与权限模型)
- [17. 可观测性与排障入口](#17-可观测性与排障入口)
- [18. 典型“不下单”排查路径](#18-典型不下单排查路径)
- [19. 测试与回放](#19-测试与回放)
- [20. 仓库目录结构](#20-仓库目录结构)
- [21. 研究数据平台 (Research Data Platform)](#21-研究数据平台-research-data-platform)
  - [21.1 定位与边界](#211-定位与边界)
  - [21.2 架构全景](#212-架构全景)
  - [21.3 快速开始](#213-快速开始)
  - [21.4 Phase 1 — 数据仓库](#214-phase-1--数据仓库)
  - [21.5 Phase 2 — 参数研究](#215-phase-2--参数研究)
  - [21.6 Phase 3-4 — 归因与执行可行性](#216-phase-3-4--归因与执行可行性)
  - [21.7 Phase 5-6 — 治理与闭环决策](#217-phase-5-6--治理与闭环决策)
  - [21.8 主交易系统整合](#218-主交易系统整合)
  - [21.9 运维与持续改进](#219-运维与持续改进)
  - [21.10 已知限制](#2110-已知限制)
  - [21.11 详细文档索引](#2111-详细文档索引)
- [22. 安全边界与风险提示](#22-安全边界与风险提示)
- [23. 开发建议](#23-开发建议)
- [24. 常见问题](#24-常见问题)

---

## 1. 项目概览

AIParticipatingAutonomousTradingSystem（AATS）是一个以**事件驱动**为基础、围绕**风控、恢复、审计、对账、可观察性**构建的交易系统原型。  
系统当前重点不是“高频盈利能力”，而是以下几个工程目标：

1. **真实市场数据可接入**
2. **决策链与执行链清晰可审计**
3. **订单生命周期可恢复、可回放、可对账**
4. **自动执行具备受保护的门禁**
5. **运行时具备 operator 控制面**
6. **当环境异常时尽量 fail-closed**

该仓库已经支持：

- 本地演示行情 + 本地 paper execution
- OKX 真实行情 + 本地 paper execution
- OKX 模拟盘 guarded submit
- 受保护的 guarded live 托管 profile

它**不是**一个“无保护、自主放大风险、可直接放心上真实资金”的成品系统。

---

## 2. 当前能力边界

### 2.1 已支持

#### `local_demo`
- 本地演示行情
- 本地 paper execution
- 适合：
  - 功能联调
  - UI 联调
  - 开发调试

#### `real_market_paper`
- OKX 真实市场行情
- 账户只读快照
- 本地 paper execution
- 适合：
  - shadow run
  - 策略观察
  - 审计链验证

#### `guarded_simulated_submit_dry_run`
- OKX 真实行情
- OKX 模拟盘账户读取
- 生成真实下单 payload，但不真正提交
- 适合：
  - 提交前联调
  - submit 载荷检查
  - operator 预演

#### `guarded_simulated_submit_enabled`
- OKX 真实行情
- OKX 模拟盘账户读取
- 模拟盘真实提交
- 前提：
  - 持久化可用
  - 恢复链可用
  - 风控门禁可用
  - operator / health / reconciliation 不阻断

#### guarded live 托管 profile
- `spot_live`
- `derivatives_live`

这些 profile 的目标是“**受保护、可观察、可恢复**的 live 运行模式”，不是完全自治模式。

---

### 2.2 明确不支持

当前仓库**不支持**以下模式：

- 无保护或自治型真实资金自动交易
- 绕过 guarded live 风控门禁的直接 live submit
- 未完成 operator / recovery / reconciliation 前置验证情况下的大资金实盘
- 把该仓库视为“已经过完整生产验证的盈利策略框架”

---

## 3. 核心设计原则

本项目围绕以下原则设计：

### 3.1 Fail-closed
当关键依赖不可确认时，系统倾向于：
- 阻断新增风险
- 保留保护性动作
- 把不确定状态暴露给 recovery / operator

### 3.2 执行真相优先于本地意图
系统区分：
- 想做什么（intent）
- 发出了什么（command / order state）
- 交易所上真实发生了什么（exchange truth）

### 3.3 恢复、对账、审计是一等公民
不是先写交易，再补恢复。  
本系统从一开始就把：
- startup recovery
- reconciliation
- unknown write handling
- operator review
纳入主设计。

### 3.4 保护性动作优先
风险降低类动作（reduce / close / flatten / protective）与非保护性新增风险动作是不同语义。  
很多门禁只应该阻断新增风险，不应该误伤退出动作。

### 3.5 配置语义要可解释
配置名应该尽量反映真实效果。  
例如：
- `strategy_sleeve_auto_execution_enabled`
比旧式的
- `strategy_sleeve_auto_parallel_enabled`
更能反映“是否允许非保护性自动执行”。

---

## 4. 系统架构总览

```text
市场数据 -> 特征计算 -> 决策上下文 -> baseline / AI -> governance / risk
      -> sleeve permission -> sleeve budget -> sleeve routing
      -> allocator -> execution planning -> order state / fill
      -> portfolio snapshot -> reconciliation / recovery -> operator / audit / UI
```

系统由多个服务模块组成，但在开发态通常通过 API gateway 统一呈现。

---

## 5. 关键主链路

### 5.1 行情与特征
1. 接收市场数据
2. 规范化行情事件
3. 计算技术特征、状态特征、成本估计、上下文快照

### 5.2 决策
1. 构建 decision context
2. baseline / AI 产生目标仓位或方向建议
3. policy / risk / mode gating 过滤不合格动作

### 5.3 Sleeve 自动执行控制
1. 判断该候选动作是否**允许自动执行**
2. 计算预算缩放是否需要收缩
3. 组合最终 route_action / delta / legs

### 5.4 分配与执行
1. allocator 只消费可执行 intent
2. execution planning 生成 execution bundle
3. order manager 管理订单状态机
4. 适配器（paper / OKX）执行提交、撤单、同步

### 5.5 持仓、对账与恢复
1. fill -> position / pnl / portfolio snapshot
2. reconciliation 修复本地状态与交易所状态不一致
3. startup recovery 在启动时主动发现未收敛状态
4. operator review 处理系统自动无法确认的剩余问题

---

## 6. 主要模块说明

### 6.1 `aats/services/market_gateway`
职责：行情接入、行情规范化、与交易所或本地 demo feed 的桥接

### 6.2 `aats/services/feature_engine`
职责：技术特征计算（波动率/趋势/流动性/regime）、费用/滑点/状态辅助特征、提供决策输入

### 6.3 `aats/services/decision_engine`
职责：baseline 决策、AI 参与式决策、决策上下文构建、策略输出标准化

### 6.4 `aats/services/governance_engine`
职责：mode / policy / risk / health / kill switch / adaptive controls、控制系统”允许做什么，不允许做什么”

### 6.5 `aats/services/strategy_engines`
职责：策略 family（independent / opportunistic / protective）、sleeve 选择、execution permission / budget / routing、allocator 前的最终自动执行控制

### 6.6 `aats/services/execution_engine`
职责：execution plan、order lifecycle、adapter（paper / OKX）、unknown write recovery、exit execution parent / child 聚合

### 6.7 `aats/services/portfolio_service`
职责：仓位、PnL、组合快照、本地状态重建

### 6.8 `aats/services/reconciliation_service`
职责：对账、repair、unresolved truth finding、运行时一致性校验

### 6.9 `aats/services/operator`
职责：UI 查询接口、控制动作、审计聚合、summary / review / health surface、RDP 查询服务

### 6.10 `aats/services/ai_service`
职责：AI 评估器（OpenAI provider）、prompt 构建、推理结果验证

### 6.11 `aats/services/execution_control`
职责：命令服务（submit/cancel/amend）、订单状态机、shadow 执行追踪、执行监控

### 6.12 `aats/services/blocker_control`
职责���策略级执行拦截、优先级管理、blocker action 分发

### 6.13 `aats/services/ledger`
职责：交易分录、lot 投影、结算 posting、funding fee 同步

### 6.14 `aats/services/recovery_control`
职责：启动恢复（startup recovery）、对账异常分类与修复策略

---

## 7. 策略与自动执行控制

这是这次仓库重构中最重要的变化之一。

### 7.1 三层自动执行控制

当前 `auto_parallel` 已拆分为三层：

#### A. 执行授权层
文件：
- `aats/services/strategy_engines/sleeve_execution_permission.py`

职责：
- 判断当前 sleeve candidate 是否允许自动进入执行链
- 区分：
  - profile deny
  - candidate disabled
  - incompatible / unsupported
  - protective override

输出：
- permission mode
- approved_for_execution
- permission reason codes

#### B. 预算控制层
文件：
- `aats/services/strategy_engines/sleeve_budget_controller.py`

职责：
- 根据 pnl / reconciliation / capacity / volatility 等因素缩放预算
- 计算 effective scale
- 生成 scaled delta / target / legs
- 输出 budget reason codes

#### C. 路由组合层
文件：
- `aats/services/strategy_engines/sleeve_routing_composer.py`

职责：
- 把 permission + budget + raw candidate 组合成最终 intent/candidate 表达
- 决定：
  - `override_target`
  - `advisory_only`
  - `hold_current`
  - `protective_execution`

### 7.2 为什么要这样拆
拆分前，系统很难回答：

- 这次没下单，是**不允许执行**
- 还是**允许执行，但预算缩成了 0**
- 还是**策略本身没过门**
- 还是**protective 动作例外放行**

现在这几层已经能结构化区分。

### 7.3 当前重要运行语义
对于非保护性 intent：

- 若 auto execution 被 profile 禁用，通常会降级为：
  - `advisory_only`
  - 或 `hold_current`
- 若批准执行但 budget 被压成 0，则会保留“approved 但 budget_zero_suppressed”的语义
- protective intent 可以绕过非保护性执行门禁

---

## 8. 执行层、恢复层与退出聚合

### 8.1 OrderState 不是唯一真相
系统明确区分：
- 本地 OrderState
- 交易所上的真实状态
- 写请求是否已确认
- 是否存在 truth pending

### 8.2 Unknown write
写请求（submit / cancel）超时或异常时，不会简单粗暴地当成“失败”。  
系统会把它提升为：
- unknown write state
- reconciliation / startup review candidate
- operator review candidate

### 8.3 ExitExecutionIntent
为支持复杂退出语义，系统引入了：
- `ExitExecutionIntent`
- `ChildExitOrderRef`

这允许系统表达：
- 一个父退出意图
- 多个子订单
- child truth pending
- parent resume block
- aggregated filled / remaining / operator review

### 8.4 Startup recovery
系统启动时会主动检查：

- 未完成的 order states
- unresolved unknown write
- exit execution parent truth pending
- missing child refs
- startup refresh failures

若关键恢复前提不满足，系统会：
- 进入 review-required
- 阻断 resume / trading

---

## 9. 运行配置模型

当前托管 profile 的配置职责已经收口成三层。

### 9.1 `aats/bootstrap/settings.py`
职责：
- 唯一 schema 真相
- 默认值
- 类型和语义定义
- 新旧配置兼容迁移

### 9.2 `configs/strategy_profiles/*.yaml`
职责：
- 策略调参
- AI
- family / sleeve / budget / trial guard 参数
- 不放凭证、不放数据库

### 9.3 根目录 `.env.*`
职责：
- 数据库
- API 端口
- 日志目录
- 交易所与 OpenAI 凭证
- 账户硬上限
- operator session / auth 相关最小 override

---

## 10. 支持的托管 Profile

### `spot`
- 现货
- guarded spot
- 默认模拟盘运行语义

### `spot_live`
- 现货
- guarded live
- 默认 OKX 实盘模式
- 仍受风控、恢复、operator 前提约束

### `derivatives`
- 合约
- guarded derivatives
- 默认模拟盘运行语义

### `derivatives_live`
- 合约
- guarded live
- 默认 OKX 实盘模式
- 仍受保护性门禁和恢复要求约束

---

## 11. 快速开始

### 11.1 创建虚拟环境并安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

### 11.2 准备 PostgreSQL
建议所有需要审计、恢复、提交验证的运行模式统一使用 PostgreSQL。

### 11.3 执行迁移

```powershell
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0001_postgres_storage.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0002_execution_and_audit_correlation.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0003_audit_execution_plan_refs.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0004_operator_users.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0005_storage_scope_columns.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0006_order_obligations.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0007_execution_outbox.sql
```

### 11.4 选择 profile
使用脚本：

```powershell
.\.venv\Scripts\python.exe scripts/start_api.py --profile spot
```

可选 profile：
- `spot`
- `derivatives`
- `spot_live`
- `derivatives_live`

---

## 12. PostgreSQL 与迁移

### 12.1 为什么建议统一使用 PostgreSQL
因为这些能力依赖持久化：

- operator auth
- audit
- reconciliation
- unknown write recovery
- exit execution parent / child 聚合
- startup recovery
- execution outbox / obligations

### 12.2 并行运行建议
如果要同时跑多个 profile：

- 使用不同数据库
- 或至少不同 `storage_scope`
- 不同 runtime lock key
- 不同 API 端口
- 不同日志目录
- 不同 operator session cookie name

---

## 13. 启动 API / UI

### 13.1 启动方式

```powershell
.\.venv\Scripts\python.exe scripts/start_api.py --profile spot --port 8000
.\.venv\Scripts\python.exe scripts/start_api.py --profile derivatives --port 8001
```

### 13.2 常用访问地址

- `http://127.0.0.1:8000/ui`
- `http://127.0.0.1:8000/system/health`
- `http://127.0.0.1:8000/system/runtime`
- `http://127.0.0.1:8000/reconciliation/latest`

---

## 14. 常见运行模式

### 14.1 本地联调
推荐：
- `spot`
- `derivatives`
- 模拟盘 / paper execution
- 快速看 UI、audit、决策和订单链是否联通

### 14.2 Shadow run
推荐：
- 真实行情
- 本地 paper execution
- 观察 decision / sleeve / allocator / execution planning

### 14.3 模拟盘 guarded submit
推荐：
- `guarded_simulated_submit_enabled`
- 检查 submit / cancel / order sync / reconciliation

### 14.4 guarded live
只在你完成以下验证后才建议使用：
- 恢复演练
- 对账演练
- operator 权限验证
- 小资金验证
- submit / cancel / fill 观察
- startup recovery 验证

---

## 15. 重要配置项说明

### 15.1 数据库
通常放在 `.env.*`：

- `AATS_DATABASE_URL`
- `AATS_DATABASE_RUNTIME_LOCK_KEY`

### 15.2 端口、日志、实例隔离
通常放在 `.env.*`：

- `AATS_API_PORT`
- `AATS_LOG_DIR`
- `AATS_OPERATOR_SESSION_COOKIE_NAME`

### 15.3 交易所与 OpenAI 凭证
通常放在 `.env.*`：

- `AATS_OKX_API_KEY`
- `AATS_OKX_API_SECRET`
- `AATS_OKX_API_PASSPHRASE`
- `AATS_OPERATOR_SESSION_SECRET`
- `AATS_OPENAI_API_KEY`

### 15.4 仓位 / 杠杆 / 上限
通常放在 `.env.*`：

- `AATS_DEFAULT_ORDER_QTY`
- `AATS_MAX_ABS_POSITION_QTY`
- `AATS_MAX_NOTIONAL_PER_SYMBOL`
- `AATS_MAX_OPEN_ORDERS`
- `AATS_MAX_TARGET_LEVERAGE`
- `AATS_DEFAULT_TARGET_LEVERAGE`

### 15.5 自动执行与 sleeve 预算
通常放在 `configs/strategy_profiles/*.yaml`：

- `strategy_family_active`
- `strategy_family_auto_selection_enabled`
- `strategy_sleeve_auto_execution_enabled`
- `strategy_sleeve_auto_min_budget_multiplier`
- `strategy_sleeve_auto_reconciliation_contraction_multiplier`
- `strategy_sleeve_auto_soft_loss_usdt`
- `strategy_sleeve_auto_hard_loss_usdt`
- `strategy_sleeve_auto_volatility_cap_enabled`

### 15.6 Unknown write 审核阈值
这类参数属于执行恢复参数，建议放 `.env.*`：

- `AATS_EXECUTION_UNKNOWN_SUBMIT_REVIEW_AFTER_SECONDS`
- `AATS_EXECUTION_UNKNOWN_CANCEL_REVIEW_AFTER_SECONDS`

---

## 16. Operator 认证与权限模型

### 16.1 当前认证模型
当 `AATS_OPERATOR_AUTH_ENABLED=true` 时：

- 浏览器会话登录基于 `operator_users` 表
- `viewer` 只读
- `operator` / `admin` 可执行控制动作
- `write API key` 仍保留为兼容管理员写权限

### 16.2 Fail-closed 启动要求
当满足以下条件时，如果没有管理员入口，系统会拒绝启动：

- 使用 PostgreSQL 持久化
- 开启 operator auth
- 开启 session 登录
- 没有启用的 admin
- 也没有 write API key

这是故意的安全设计，防止系统启动后实际没人能管它。

### 16.3 已有保护
- 禁止删除自己
- 禁止禁用自己
- 禁止移除最后一个启用管理员
- 被禁用用户的现有会话会失效

---

## 17. 可观测性与排障入口

### 17.1 关键 API

#### 系统态
- `GET /system/health`
- `GET /system/runtime`

#### 决策与执行
- `GET /decision/latest`
- `GET /orders/recent`
- `GET /fills/recent`
- `GET /execution/errors`

#### 组合与对账
- `GET /portfolio/latest`
- `GET /reconciliation/latest`
- `GET /audit/latest`

### 17.2 日志
默认日志目录在 `logs/` 下。  
建议至少保留：

- 主运行日志
- 审计日志
- 关键错误日志
- 启动恢复日志

### 17.3 现在应该优先看的 runtime 字段
在这次重构后，排 sleeve 自动执行时建议优先看：

- `entry_execution_guard`
- `entry_auto_execution_enabled`
- `execution_control_mode_counts`
- `execution_behavior_counts`
- `budget_zero_suppression_count`

而不是只看旧式的 `automation_state`。

---

## 18. 典型“不下单”排查路径

这是最常见的运维问题之一。

### 18.1 第一步：先确认是否有 signal
看：
- decision audit
- strategy_sleeve_intents
- long/short score
- blocking_reasons

如果根本没有 opening/selectable 信号，那就不是执行链问题。

### 18.2 第二步：看 sleeve 自动执行是否拒绝
看：
- `permission_mode`
- `approved_for_execution`
- `entry_execution_guard`
- `permission_reason_codes`

典型情况：
- profile 禁用了非保护性自动执行
- candidate 不兼容
- runtime 不支持

### 18.3 第三步：看 budget 是否被压成 0
看：
- `budget_zero_suppressed`
- `budget_reason_codes`
- `effective_scale`

如果批准执行但预算缩到 0，会出现：
- approved
- 但 route/actionable 最终被 budget suppress

### 18.4 第四步：看 allocator 是否还有可执行 delta
如果：
- `route_action = advisory_only`
- 或 `delta_position_qty = 0`

allocator 通常不会继续生成 execution plan。

### 18.5 第五步：看 execution layer 是否真的卡住
若 allocator 已生成：
- execution plan
- strategy execution bundle

再查：
- order manager
- adapter
- reconciliation
- unknown write
- health / kill switch

---

## 19. 测试与回放

### 19.1 当前测试目录
- `tests/unit`
- `tests/integration`
- `tests/replay`
- `tests/scenario`

### 19.2 建议优先补的测试
如果你继续迭代这个系统，建议保持这几类测试不断裂：

1. sleeve orchestration tests  
2. execution gating tests  
3. unknown write recovery tests  
4. startup recovery tests  
5. exit execution parent / child aggregation tests  
6. reconciliation truth pending tests  

### 19.3 回放能力
仓库包含：
- replay
- scenario
- event archive
相关脚本，可用于验证：
- 决策链
- 恢复链
- operator review 语义
- 对账收敛

---

## 20. 仓库目录结构

```text
aats/
  api/                    # API 层与前端静态资源
  bootstrap/              # settings / profile / env / config / active_parameters
  bus/                    # 事件总线与消息模型
  events/                 # 事件定义
  schemas/                # 运行时 schema / DTO（24 文件）
  services/
    market_gateway/       # 行情接入
    feature_engine/       # 特征计算（波动率/趋势/流动性/regime）
    decision_engine/      # baseline / AI 决策
    governance_engine/    # policy / risk / health / kill switch / adaptive controls
    strategy_engines/     # strategy family / sleeve / allocator / auto control
      families/           #   independent / opportunistic / protective
      independent/        #   independent 策略引擎（16 文件）
      smart_arbitrage/    #   套利策略引擎（9 文件）
    execution_engine/     # order manager / adapters / exit aggregation / recovery
    execution_control/    # 命令服务 / 订单状态机 / shadow / 监控
    portfolio_service/    # position / pnl / snapshots
    reconciliation_service/ # 对账与修复
    recovery_control/     # 启动恢复 / 对账分类
    operator/             # operator query / action / summary / RDP queries
    ai_service/           # AI 评估器 / prompt / inference
    blocker_control/      # 策略级执行拦截
    ledger/               # 交易分录 / lot / settlement / funding fee
  storage/                # PostgreSQL / SQLAlchemy 持久化实现（46 文件）
  data_platform/          # 研究数据平台（数仓，详见第 21 章）
    config.py             #   Pydantic 配置，从 .env.research 读取
    db.py                 #   数据库连接池 + migration runner
    models.py             #   数据模型 + 表名解析
    live_query_adapter.py #   Live DB 只读查询适配器
    collectors/           #   数据采集（backfill + rolling）
    normalize/            #   symbol 映射 + 时间标准化
    validate/             #   质量检查 + 报告写入
    merge/                #   staging -> bronze -> silver 合并管道
    gold/                 #   funding 对齐 + Gold replay bar 构建
    jobs/                 #   scheduler / checkpoint / run registry / gap repair
    replay/               #   Phase 2 逐 bar 重放引擎 + 适配器 + 扫描
    attribution/          #   Phase 3 归因分析
    execution_realism/    #   Phase 4 执行可行性
    governance/           #   Phase 5 治理（artifact / registry / quality）
    decision_system/      #   Phase 6 闭环决策
    production_workflow/  #   工作流调度 / pre-apply gate
    operations/           #   失败恢复 / 可靠性检查 / 告警 / 环境守卫
    metrics/              #   持续改进指标 / 基线比较 / 发布评估

configs/
  base.yaml               # 主配置（含所有默认值）
  dev.yaml / staging.yaml / prod.yaml
  strategy_profiles/       # 托管 profile 的策略调参（spot/derivatives/live）
  active_parameter_sets/   # RDP 回灌的 active parameter set
  rdp_workflows/           # RDP 工作流 JSON 配置
  research_batches/        # 校准批次 JSON 模板（16 个 batch 文件）
  research_rounds/         # Step 2 研究轮次配置
  templates/               # .env 示例模板

migrations/
  0001_postgres_latest_schema.sql       # 主系统 schema（最新版合并）
  0002_postgres_legacy_upgrade.sql      # 旧版升级迁移
  0003_postgres_execution_attempt_id_columns.sql
  0004_postgres_exit_execution_repository.sql
  research/               # 研究数据平台迁移 SQL（0001-0012）

data/historical/          # 历史数据目录
  incoming/               #   放入 ZIP 文件，daemon 自动消费
  completed/              #   消费成功后自动移入
  failed/                 #   消费失败后自动移入（附 .error 日志）

artifacts/
  research/               # Phase 2-4 研究产物
    experiments/           #   replay decisions, diagnostics, reports
    calibration_batches/   #   校准批次产物
    calibration_rounds/    #   Step 1 校准轮次
    step2_rounds/          #   Step 2 研究轮次
    attribution_rounds/    #   Phase 3 归因轮次
    execution_rounds/      #   Phase 4 执行可行性轮次
  governance/              # Phase 5 治理产物
    artifact_index.json / current_parameter_registry.json / quality_monitor_summary.json
  decision_system/         # Phase 6 决策注册表
    recommendation_registry.json / active_decision_registry.json
  decision_rounds/         # Phase 6 决策 round 产物（per round_id）
  production_workflow/     # 参数发布 / gate / 观察记录
  metrics/                 # 指标快照 / 历史 / 改进积压
  operations/              # 工作流运行记录 / 失败记录 / 告警
  reviews/                 # 周/月评审报告

docs/
  rdp/                    # RDP 技术详细参考（3 文件）
  operations/             # RDP 运营文档（21 文件）
  configuration/          # 配置职责说明
  task*/                  # 历史任务设计与演进文档

scripts/                  # 61 个 Python 脚本
  # ── 主系统 ──
  start_api.py / generate_managed_config_artifacts.py / archive_event_store.py
  # ── RDP Phase 1: 数据 ──
  rdp_start.py / rdp_historical_daemon.py / rdp_realtime_daemon.py
  rdp_build_gold.py / rdp_build_gold_all.py / rdp_detect_gaps.py
  # ── RDP Phase 2: 研究 ──
  rdp_run_replay.py / rdp_run_parameter_scan.py / rdp_run_calibration_batch.py
  rdp_run_step1_calibration.py / rdp_run_step2_research.py
  # ── RDP Phase 3-4: 归因与执行 ──
  rdp_run_live_attribution.py / rdp_run_phase3_round.py
  rdp_run_execution_realism.py / rdp_run_phase4_round.py
  # ── RDP Phase 5: 治理 ──
  rdp_validate_artifacts.py / rdp_build_artifact_index.py / rdp_freeze_parameter_set.py
  rdp_list_active_rounds.py / rdp_retry_failed_round.py / rdp_run_quality_monitor.py
  # ── RDP Phase 6: 决策 ──
  rdp_run_decision_round.py / rdp_select_parameter_upgrade.py
  rdp_evaluate_promotion_readiness.py / rdp_update_decision_registry.py
  # ── RDP 整合: 审批/应用/发布 ──
  rdp_check_live_db.py / apply_active_parameter_set.py / approve_recommendation_and_apply.py
  rdp_approve_recommendation.py / rdp_apply_approved_recommendation.py
  rdp_create_parameter_release.py / rdp_run_pre_apply_gate.py
  rdp_rollback_active_parameter_set.py / rdp_run_post_apply_observation.py
  # ── RDP 运维: 调度/指标/可靠性 ──
  rdp_run_scheduled_workflow.py / rdp_build_metrics_snapshot.py
  rdp_compare_release_to_baseline.py / rdp_evaluate_release_effectiveness.py
  rdp_run_periodic_review.py / rdp_generate_improvement_backlog.py
  rdp_run_reliability_check.py / rdp_build_alert_summary.py
tests/                    # 单元 / 集成 / 回放 / 场景测试
```

---

## 21. 研究数据平台 (Research Data Platform)

研究数据平台（RDP）是独立于交易系统主链路的**离线参数研究子系统**。它从数据采集、参数扫描、归因分析、执行可行性验证到闭环决策推荐，形成完整的参数优化管线，最终通过受控审批流程将研究结论回灌到主交易系统。

### 21.1 定位与边界

| 维度 | 说明 |
|------|------|
| 职责 | 离线/准实时数据采集 → 参数研究 → 归因分析 → 执行验证 → 治理 → 闭环决策 → 受控回灌 |
| 数据源 | OKX 历史文件下载（ZIP/CSV） + OKX REST API 增量 + Production DB 只读 |
| 数据库 | 独立库 `aats_research`（6 个 schema：meta/staging/bronze/silver/gold/research） |
| 配置文件 | `.env.research`（`RDP_` 前缀），不与交易系统 `.env.*` 混用 |
| 核心原则 | 不侵入实时主链 · 旁路分析 + 受控回灌 · 研究与生产分库 · 人工审批 |

### 21.2 架构全景

#### 七阶段研究管线

```text
Phase 1        Phase 2         Phase 3          Phase 4
数据仓库  ───→  参数研究  ───→  归因分析  ───→  执行可行性
(采集/清洗/     (replay/        (replay vs       (成交/滑点/
 分层存储)       扫描/校准)      live 对照)       成本验证)
    │                                               │
    │           Phase 5         Phase 6             │
    └────────→  治理  ────────→  闭环决策  ←────────┘
               (版本/质量/       (评分/推荐/
                artifact)        readiness)
                                    │
                              Integration
                            ───→ 主系统整合
                               (审批/回灌/
                                API/监控)
```

| 阶段 | 核心问题 | 主要产物 |
|------|---------|---------|
| Phase 1 | 数据从哪来？怎么保证质量？ | Silver 标准化行情 + Gold replay bars |
| Phase 2 | 参数变化如何影响策略决策结构？ | 诊断报告 + parameter candidates |
| Phase 3 | 为什么 live 没有按 replay 预期下单？ | 归因瀑布 + failure modes |
| Phase 4 | 这笔单在真实市场能成交吗？成本多少？ | 成交可行性 + cost-adjusted edge |
| Phase 5 | 产物可追溯吗？质量达标吗？ | artifact 索引 + 质量巡检 + 参数 registry |
| Phase 6 | 哪组参数值得推上线？ | recommendations + readiness report |
| Integration | 如何安全地把研究结论用到生产？ | active parameter sets + 审批 log |

#### 五层数据架构

```text
meta      — 元数据：运行记录、checkpoint、质量报告、文件注册
staging   — 原始入库层，保留 raw_symbol / raw_ts / source_file_id 全链路溯源
bronze    — 去重 upsert 层，PK=(symbol, ts)，保留原始字段
silver    — 标准化层，经过质量验证的规范数据
gold      — 回放层，candle + funding rate as-of join 对齐后的 replay bars
research  — 参数研究层，实验元数据、诊断摘要、扫描批次记录
```

共 44 张表，通过 `migrations/research/0001-0012` SQL 文件管理。

#### 数据流全景

```text
+-- 历史数据 daemon --------+    +-- 实时数据 daemon --------+
|  incoming/ ZIP            |    |  OKX REST API             |
|  → file discovery         |    |  → candles/funding 采集    |
|  → staging → 质量门控     |    |  → staging → 质量门控      |
|  → bronze → silver        |    |  → bronze → silver         |
|  → 移到 completed/failed  |    |  → 定期 Gold 构建 + Gap 修复|
+---------------------------+    +----------------------------+
              ↓                                ↓
        Silver 标准化层  ──→  Gold 回放层  ──→  Phase 2-6 研究管线
```

### 21.3 快速开始

```powershell
# 1. 配置
cp configs/templates/.env.research.example .env.research
# 编辑 .env.research，填入 RDP_DATABASE_URL

# 2. 初始化数据库（自动建库 + 迁移 0001-0012）
python scripts/rdp_init_db.py

# 3. 放入历史数据（按目录约定放入 ZIP）
# data/historical/incoming/candles_swap/1m/BTC-USDT-SWAP-candlesticks-*.zip

# 4. 一键启动（历史消费 + 实时采集同时运行）
python scripts/rdp_start.py

# 5. 如果 Silver 有数据但 Gold 为空，手动触发 Gold 构建
python scripts/rdp_build_gold_all.py --dry-run   # 预览
python scripts/rdp_build_gold_all.py              # 构建

# 6. 运行参数研究（可选）
python scripts/rdp_run_replay.py \
    --family independent --symbol BTC-USDT-SWAP --timeframe 1m \
    --start 2026-03-31 --end 2026-04-02 --dataset-version v1.0
```

**关键配置变量**（`.env.research`）：

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `RDP_DATABASE_URL` | Research PostgreSQL 连接串 | `postgresql+psycopg://localhost:5432/aats_research` |
| `RDP_LIVE_DATABASE_URL` | Production DB 只读连接（Phase 3+ 需要） | — |
| `RDP_HISTORICAL_INCOMING_DIR` | 历史 ZIP 输入目录 | `./data/historical/incoming` |
| `RDP_ROLLING_CANDLES_SYMBOLS` | 采集 symbol 列表 | `BTC-USDT,ETH-USDT,BTC-USDT-SWAP,ETH-USDT-SWAP` |
| `RDP_ROLLING_CANDLES_TIMEFRAMES` | 采集 timeframe 列表 | `1m,5m,15m,1H` |
| `RDP_ENV` | 环境标识（dev/staging/prod） | `dev` |

完整配置模板：`configs/templates/.env.research.example`

### 21.4 Phase 1 — 数据仓库

Phase 1 负责从 OKX 采集市场数据，经清洗、去重、标准化后存入分层数据仓库，最终生成可直接用于回测的 Gold replay bars。

**覆盖范围**：OKX · BTC-USDT / ETH-USDT / BTC-USDT-SWAP / ETH-USDT-SWAP · 1m / 5m / 15m / 1H · candles + funding rates

**数据采集**：

| daemon | 职责 | 默认间隔 |
|--------|------|----------|
| Historical | 定时扫描 `incoming/` 目录，发现新 ZIP 自动消费 → staging → silver → Gold | 30 秒 |
| Realtime | 滚动采集 candles/funding + 定期构建 Gold + 定期检测 gap | 60 秒 |

**目录约定**：将 ZIP 放入 `data/historical/incoming/{candles_spot,candles_swap,funding_swap}/{1m,5m,15m,1h}/`，timeframe 由子目录名自动推断。消费成功移入 `completed/`，失败移入 `failed/`（附 `.error` 日志）。

**质量门控**：candle 检查重复/OHLC 非法/时间乱序（fail 级）+ 缺失间隔/volume 负值（warn 级）；funding 检查重复/乱序/null rate。质量报告写入 `meta.quality_reports`。

**Gold 层**：通过 as-of join 将 funding rate 对齐到 candle bar。daemon 在消费新 swap ZIP 时自动构建；也可手动 `python scripts/rdp_build_gold_all.py`。Spot Gold 表按设计为空（spot 无 funding rate）。

### 21.5 Phase 2 — 参数研究

Phase 2 在 Gold 数据之上构建参数研究闭环，回答"当参数变化时，策略行为结构如何改变"。

**核心概念**：

- **Replay 引擎**：逐 bar 重放，调用 family adapter（independent / directional），输出结构化决策
- **统一 Edge Contract**：所有 adapter 输出 4 层 edge 分解 — `net_edge = signal + funding - cost`（bps）
- **参数扫描**：参数网格笛卡尔积探索（`rdp_run_parameter_scan.py`）
- **校准批处理**：JSON 文件驱动的少量校准实验（`rdp_run_calibration_batch.py`）
- **Step 1 校准**：independent / BTC-USDT-SWAP / 15m 单范围自动校准（`rdp_run_step1_calibration.py`）
- **Step 2 闭环**：覆盖 {independent, directional} × {15m, 1H} 的完整研究闭环（`rdp_run_step2_research.py`）

**关键脚本**：

| 脚本 | 用途 |
|------|------|
| `rdp_run_replay.py` | 单次 replay 实验 |
| `rdp_run_parameter_scan.py` | 参数网格批量扫描（默认 27 组合） |
| `rdp_run_calibration_batch.py` | JSON 驱动的校准批处理 |
| `rdp_run_step1_calibration.py` | Step 1 自动化校准编排 |
| `rdp_run_step2_research.py` | Step 2 完整研究闭环（4 阶段） |

**产物目录**：`artifacts/research/experiments/`、`artifacts/research/calibration_batches/`、`artifacts/research/step2_rounds/`

> 详细参考：[Phase 2 参数研究详细文档](docs/rdp/phase2_parameter_research_details.md)

### 21.6 Phase 3-4 — 归因与执行可行性

**Phase 3（Live Attribution）** 建立 replay vs live 对照归因，回答"为什么 live 没下单"。

- 8 层瀑布归因：Strategy → Permission → Allocator → Budget → Risk → Execution → Order → Fill
- 停在第一层失败处，聚合 top failure modes
- 脚本：`rdp_run_live_attribution.py`（单次）/ `rdp_run_phase3_round.py`（批量 4 组合）

**Phase 4（Execution Realism）** 进入市场微观结构层，回答"这笔单能成交吗、成本多少"。

- V1 分析链：Gold bar matching → fill feasibility → slippage estimate → cost-adjusted edge
- V1 滑点模型：half-spread + sqrt(volume_ratio) impact（bar-proxy，透明可解释）
- 脚本：`rdp_run_execution_realism.py`（单次）/ `rdp_run_phase4_round.py`（批量 4 组合）

**产物目录**：`artifacts/research/attribution_rounds/`、`artifacts/research/execution_rounds/`

> 详细参考：[Phase 3-4 归因与执行可行性详细文档](docs/rdp/phase3_4_attribution_execution_details.md)

### 21.7 Phase 5-6 — 治理与闭环决策

**Phase 5（Governance）** 将平台从"能跑"推进到"可长期运行、可版本治理、可追溯"。

- Artifact 规范化 + 全局索引（`artifact_index.json`）
- 参数生命周期：`draft → candidate → frozen → deprecated`
- Round 状态管理：`pending → running → succeeded / partial_success / failed`
- 四维质量巡检：artifact / 结果 / 参数 / 治理层（`rdp_run_quality_monitor.py`）
- 失败 round 重跑计划（`rdp_retry_failed_round.py`）

**Phase 6（Decision System）** 整合所有 Phase 的证据，生成统一的生产决策建议。

- 跨 Phase 2/3/4/5 证据收集（`evidence_bundle.py`）
- 4 维度参数评分（研究 3 + 归因 2 + 执行 2 + 治理 2 = 满分 9）
  - score_ratio ≥ 0.7 → promote · 0.4~0.7 → hold · < 0.4 → reject
- Family/Timeframe 状态决策：keep_active / lower_priority / pause / require_review
- 7 项 Promotion Readiness check
- 脚本：`rdp_run_decision_round.py`（完整闭环）

> 详细参考：[运营手册](docs/operations/platform_runbook.md) · [参数治理](docs/operations/parameter_governance.md) · [Operator 检查清单](docs/operations/operator_checklist.md)

### 21.8 主交易系统整合

将 RDP 从独立研究平台整合为主交易系统的正式旁路子系统。

```text
                      +---------------------------+
                      |   主交易系统 (Realtime)    |
                      |   Market → Feature →       |
                      |   Decision → Governance →  |
                      |   Strategy → Execution     |
                      +-------------+-------------+
                                    |
                                    | 事实数据(只读) / 参数回灌
                                    v
                +---------------------------------------------+
                | RDP (Phase 1~6 + Integration Layer)         |
                +-------------------+-------------------------+
                                    |
                                    | approved parameter set
                                    v
                    +--------------------------------------+
                    |   configs/active_parameter_sets/     |
                    +--------------------------------------+
```

**四阶段整合**：

| 阶段 | 内容 | 核心交付物 |
|------|------|-----------|
| A. 事实数据对接 | RDP 只读访问 production DB 7 张表 | `live_query_adapter.py` |
| B. 参数回灌机制 | Active parameter set 加载与注入 | `active_parameters.py`、`apply_active_parameter_set.py` |
| C. Operator 可见性 | 8 个 RDP 只读 API 端点（`/rdp/` 前缀） | `rdp_routes.py`、`rdp_queries.py` |
| D. 受控应用流程 | Recommendation → Pre-apply Gate → Approval → Apply | `approve_recommendation_and_apply.py` |

**API 端点一览**（`/rdp/` 前缀）：

| 端点 | 说明 |
|------|------|
| `GET /rdp/health` | RDP 子系统健康状态 |
| `GET /rdp/parameters/active` | 当前 active parameter sets |
| `GET /rdp/attribution/latest` | 最近 attribution 结论 |
| `GET /rdp/execution/latest` | 最近 execution realism 结论 |
| `GET /rdp/decisions/latest` | 当前 family/tf 决策状态 |
| `GET /rdp/recommendations/latest` | 最近 recommendations |
| `GET /rdp/decision-round/latest` | 最近 decision round 结论 |
| `GET /rdp/readiness` | Promotion readiness 评估 |

**整合原则**：不侵入实时主链 · 旁路分析 + 受控回灌 · 研究与生产分库 · 建议与应用分离 · 第一版不做自动 apply

### 21.9 运维与持续改进

RDP 通过以下机制支持长期可靠运行：

**工作流调度**：JSON 配置驱动的 workflow dispatcher 支持 4 种工作流类型（research_round / decision_round / quality_check / maintenance）。详见 [调度策略](docs/operations/rdp_scheduling_strategy.md) · [工作流日历](docs/operations/rdp_workflow_calendar.md)。

**失败恢复**：failure registry 记录 + retry manager 支持单任务/整工作流重试 + 自动故障录入。

**可靠性**：7 项 reliability check（质量监控/活跃决策/工作流配置/产物目录/开放故障/发布历史/活跃参数）+ alert summary 构建与历史管理。详见 [可靠性 Runbook](docs/operations/rdp_reliability_runbook.md)。

**环境隔离**：dev / staging / prod 三环境策略矩阵，通过 `RDP_ENV` 控制参数应用、回滚、工作流执行、直接 DB 访问的权限。详见 [环境矩阵](docs/operations/rdp_environment_matrix.md)。

**持续改进指标**：5 层 24 个指标（研究/归因/执行/运维/可靠性）+ 基线比较（3 种策略）+ 发布有效性评估（4 维度）+ 周/月周期性评审 + 改进积压自动检测（6 个来源）。

**参数映射语义**：RDP 参数名与主系统 `AATSSettings` 字段名的映射关系分为 DIRECT / APPROXIMATE / PLACEHOLDER 三类，详见 [参数映射参考](docs/operations/parameter_mapping_reference.md)。

### 21.10 已知限制

| 项目 | 说明 |
|------|------|
| Scheduler 防重 | 进程内 in-memory bucket dedup，重启后可能重跑一次（下游 upsert 幂等） |
| Symbol 白名单 | 硬编码 4 个 instrument，后续需改为数据库驱动 |
| Gold volume 语义 | spot vol = 基础币量，swap vol = 合约张数，未做跨类型统一 |
| 单进程 | scheduler 设计为单进程，不支持多 worker 并行 |
| Replay 评分 | Phase 2 使用简化评分模型（不含 AI assessment），与生产存在偏差 |
| Replay 撮合 | Phase 2 不含撮合仿真和 PnL accounting |
| Signal 校准 | `signal_edge_scale_bps` 当前为经验性默认值（10.0），尚未经过历史数据校准 |
| Execution realism V1 | Phase 4 无 orderbook depth / trades 数据，spread 和 impact 基于 bar OHLCV proxy |
| 仓位极小 | BTC-USDT-SWAP 1 合约 = 0.01 BTC，volume ratio 接近 0，feasibility 指标在小仓位下区分度有限 |

### 21.11 详细文档索引

| 文档 | 内容 |
|------|------|
| **技术细节** | |
| [`docs/rdp/phase2_parameter_research_details.md`](docs/rdp/phase2_parameter_research_details.md) | Phase 2 完整参考：Edge Contract、成本模型、参数、CLI、产物、适配器 |
| [`docs/rdp/phase3_4_attribution_execution_details.md`](docs/rdp/phase3_4_attribution_execution_details.md) | Phase 3-4 完整参考：归因瀑布、执行可行性、滑点模型、CLI |
| [`docs/rdp/module_reference.md`](docs/rdp/module_reference.md) | 全部代码模块职责清单（Phase 1~6 + Integration + Operations + Metrics） |
| **运维 — 日常操作** | |
| [`docs/operations/rdp_operator_workflow.md`](docs/operations/rdp_operator_workflow.md) | Operator 完整 SOP：查看/审批/应用/观察/回滚 |
| [`docs/operations/operator_checklist.md`](docs/operations/operator_checklist.md) | 日常巡检 + 运行前后检查 + 交接须知 |
| [`docs/operations/platform_runbook.md`](docs/operations/platform_runbook.md) | 平台全景 + 日常操作 + 故障排查 |
| [`docs/operations/operator_rdp_integration.md`](docs/operations/operator_rdp_integration.md) | RDP API 端点参考 + 数据源映射 |
| **运维 — 参数治理** | |
| [`docs/operations/parameter_governance.md`](docs/operations/parameter_governance.md) | 参数生命周期（draft→frozen→deprecated） |
| [`docs/operations/parameter_apply_and_rollback.md`](docs/operations/parameter_apply_and_rollback.md) | 参数应用与回滚操作指南 |
| [`docs/operations/production_parameter_change_runbook.md`](docs/operations/production_parameter_change_runbook.md) | 生产参数变更全流程（gate→release→observation→rollback） |
| [`docs/operations/parameter_mapping_reference.md`](docs/operations/parameter_mapping_reference.md) | RDP↔主系统参数映射语义 |
| **运维 — 调度与可靠性** | |
| [`docs/operations/rdp_scheduling_strategy.md`](docs/operations/rdp_scheduling_strategy.md) | 工作流调度策略 + JSON 配置 + Cron 示例 |
| [`docs/operations/rdp_reliability_runbook.md`](docs/operations/rdp_reliability_runbook.md) | 可靠性 Runbook + 异常 SOP |
| [`docs/operations/rdp_environment_matrix.md`](docs/operations/rdp_environment_matrix.md) | 环境隔离权限矩阵（dev/staging/prod） |
| [`docs/operations/rdp_metrics_framework.md`](docs/operations/rdp_metrics_framework.md) | 5 层 24 指标框架 |
| **规范文档** | |
| [`docs/operations/artifact_conventions.md`](docs/operations/artifact_conventions.md) | 目录结构 + manifest 规范 |
| [`docs/operations/round_lifecycle.md`](docs/operations/round_lifecycle.md) | Round 状态定义 + 退出码 + 失败处理 |
| [`docs/operations/live_schema_contract_for_rdp.md`](docs/operations/live_schema_contract_for_rdp.md) | Live DB 7 张表结构契约 |

---

## 22. 安全边界与风险提示

### 22.1 这不是可以直接放大规模运行的成品系统
你需要自己验证：

- 策略经济性
- 费用模型
- submit / cancel 稳定性
- 恢复与对账稳定性
- operator 响应机制

### 22.2 对真实资金尤其要保守
建议顺序：

1. local demo  
2. real market paper  
3. simulated submit dry-run  
4. simulated submit enabled  
5. 小资金 guarded live  
6. 长时间观察后再扩大范围

### 22.3 不要绕过受保护路径
当前系统的很多安全设计都建立在：
- guarded live
- operator
- recovery
- reconciliation
- health checks
之上。

如果你绕过这些层，很多假设都会失效。

---

## 23. 开发建议

### 23.1 新增配置字段时
优先改：
- `aats/bootstrap/settings.py`

然后再决定：
- 放 `.env.*`
- 还是 `strategy_profiles/*.yaml`

### 23.2 修改自动执行语义时
尽量保持三层边界：

- permission
- budget
- composition

不要再把三者重新揉回 `auto_parallel.py`。

### 23.3 修改执行层时
优先考虑：
- unknown write
- recovery
- reconciliation
- startup safe-to-trade

而不是只看 happy path submit 成功。

### 23.4 修改 operator/query 时
尽量优先暴露：
- 新语义字段
- 兼容字段退居次要

---

### 23.5 修改数仓代码时
优先考虑：
- 质量门控不可绕过
- merge pipeline 幂等性（upsert on PK）
- checkpoint 只在确认写入后推进
- meta 表状态可追踪（不允许静默跳过）
- 新增 timeframe / symbol 需同步更新 migration + models.py

---

## 24. 常见问题

### Q1：系统在跑，为什么就是不下单？
先查四件事：

1. 是否真的有 opening/selectable 信号  
2. `approved_for_execution` 是否为真  
3. 是否 `budget_zero_suppressed`  
4. allocator 是否拿到非 0 delta

### Q2：为什么有信号、有 score，也不一定下单？
因为信号出现后还要经过：
- permission
- budget
- composition
- allocator
- execution health / recovery / risk

### Q3：为什么 protective 动作和 opening 动作处理不一样？
因为保护性动作的目标是降低风险。  
很多门禁应阻断新增风险，但不应误伤退出动作。

### Q4：为什么系统启动后会拒绝进入可交易状态？
通常是：
- startup recovery 未收敛
- operator auth 前置不满足
- exit execution truth pending
- unknown write aged review required
- reconciliation / health / runtime guard 未通过

### Q5：现在最值得看的配置文档在哪里？
- `configs/README.md`
- `docs/configuration/managed-config-reference.md`

---

## 附：建议的维护策略

如果你准备长期维护这个仓库，建议把 README 当成两层文档：

### README 主体
保留：
- 架构
- 配置
- 运行方式
- 排障入口
- 风险边界

### docs/ 子文档
放更细的：
- task 设计演进
- 特定恢复策略
- 特定 family 设计
- SQL / migration 细节
- operator API 细节

这样 README 会保持“详细但不失控”。

