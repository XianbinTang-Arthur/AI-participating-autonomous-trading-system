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
职责：
- 行情接入
- 行情规范化
- 与交易所或本地 demo feed 的桥接

### 6.2 `aats/services/feature_engine`
职责：
- 技术特征计算
- 费用/滑点/状态辅助特征
- 提供决策输入

### 6.3 `aats/services/decision_engine`
职责：
- baseline 决策
- AI 参与式决策
- 决策上下文构建
- 策略输出标准化

### 6.4 `aats/services/governance_engine`
职责：
- mode / policy / risk / health / kill switch
- 控制系统“允许做什么，不允许做什么”

### 6.5 `aats/services/strategy_engines`
职责：
- 策略 family
- sleeve 选择
- execution permission / budget / routing
- allocator 前的最终自动执行控制

### 6.6 `aats/services/execution_engine`
职责：
- execution plan
- order lifecycle
- adapter（paper / OKX）
- unknown write recovery
- exit execution parent / child 聚合

### 6.7 `aats/services/portfolio_service`
职责：
- 仓位
- PnL
- 组合快照
- 本地状态重建

### 6.8 `aats/services/reconciliation_service`
职责：
- 对账
- repair
- unresolved truth finding
- 运行时一致性校验

### 6.9 `aats/services/operator`
职责：
- UI 查询接口
- 控制动作
- 审计聚合
- summary / review / health surface

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
  bootstrap/              # settings / profile / env / config glue
  bus/                    # 事件总线与消息模型
  events/                 # 事件定义
  schemas/                # 运行时 schema / DTO
  services/
    market_gateway/       # 行情接入
    feature_engine/       # 特征计算
    decision_engine/      # baseline / AI 决策
    governance_engine/    # policy / risk / health / kill switch
    strategy_engines/     # strategy family / sleeve / allocator / auto control
    execution_engine/     # order manager / adapters / exit aggregation
    portfolio_service/    # position / pnl / snapshots
    reconciliation_service/ # 对账与修复
    operator/             # operator query / action / summary
  storage/                # PostgreSQL / SQLAlchemy 持久化实现
  data_platform/          # 研究数据平台（数仓，详见第 21 章）
    config.py             #   Pydantic 配置，从 .env.research 读取
    db.py                 #   数据库连接池 + migration runner
    models.py             #   数据模型 + 表名解析
    collectors/           #   数据采集
      backfill/           #     历史回填（ZIP 文件发现/注册/解析/入库）
      rolling/            #     API 增量采集（candles + funding）
    normalize/            #   symbol 映射 + 时间标准化
    validate/             #   质量检查 + 报告写入
    merge/                #   staging -> bronze -> silver 合并管道
    gold/                 #   funding 对齐 + Gold replay bar 构建
    jobs/                 #   scheduler / checkpoint / run registry / gap repair
    test_data/            #   验收用 OKX 真实数据

apps/
  api_gateway/
  decision_engine/
  execution_engine/
  feature_engine/
  governance_engine/
  market_gateway/
  portfolio_service/
  reconciliation_service/

configs/
  strategy_profiles/      # 托管 profile 的策略调参
  templates/              # .env 示例模板（含 .env.research.example）

docs/
  configuration/          # 配置职责说明
  task*/                  # 任务设计与演进文档

migrations/               # 交易系统 PostgreSQL 迁移 SQL
  research/               # 研究数据平台迁移 SQL（0001-0011）
scripts/                  # 启动、seed、回放、报告脚本
  rdp_run_backfill.py     #   数仓历史回填入口
  rdp_phase1_acceptance.py #  数仓 Phase 1 验收脚本
tests/                    # 单元 / 集成 / 回放 / 场景测试
```

---

## 21. 研究数据平台 (Research Data Platform)

研究数据平台（RDP）是独立于交易系统主链路的**离线数据仓库**模块，负责从 OKX 采集、清洗、标准化市场数据，最终生成可直接用于回测/回放分析的 Gold 行情。

### 21.1 定位与边界

| 维度 | 说明 |
|------|------|
| 职责 | 离线/准实时数据采集、清洗、分层存储、质量验证 |
| 数据源 | OKX 历史文件下载（ZIP/CSV） + OKX REST API 增量 |
| 输出 | Silver 标准化行情 + Gold 回放级 replay bars |
| 数据库 | 独立库 `aats_research`，与交易系统分库隔离 |
| 配置文件 | `.env.research`（`RDP_` 前缀），不与交易系统 `.env.*` 混用 |

### 21.2 五层数据架构

```text
meta     ← 元数据：运行记录、checkpoint、质量报告、文件注册
staging  ← 原始入库层，保留 raw_symbol / raw_ts / source_file_id 全链路溯源
bronze   ← 去重 upsert 层，PK=(symbol, ts)，保留原始字段
silver   ← 标准化层，经过质量验证的规范数据
gold     ← 回放层，candle + funding 对齐后的 replay bars
```

共 41 张表，通过 `migrations/research/0001-0011` SQL 文件管理。

### 21.3 数据流全景

#### 历史回填 (Backfill)

```text
OKX ZIP 文件
  → file_discovery（扫描、SHA256 去重、注册到 meta.raw_source_files）
  → file_parser（解析 CSV，header 标准化，BOM/引号容错）
  → staging 写入
  → candle_quality_checker / funding_quality_checker（质量门控）
  → bronze_merger（staging -> bronze，upsert）
  → silver_merger（bronze -> silver，upsert）
```

运行入口：`python scripts/rdp_run_backfill.py --dir <目录> [--timeframe 1m]`

#### 滚动增量 (Rolling)

```text
OKX REST API
  → candles_api_collector / funding_api_collector
  → 分页抓取（newest-first, backward pagination）
  → _dedupe_candle_rows / _dedupe_funding_rows（页边界去重）
  → checkpoint 过滤（只取 > last_successful_ts 的新数据）
  → staging 写入
  → 质量验证 + merge pipeline
  → checkpoint 推进
```

由 `scheduler.py` 驱动，每 60 秒 tick，按 cadence 触发：
- 1m candles：每 1 分钟
- 5m candles：每 5 分钟
- 15m candles：每 15 分钟
- 1H candles：每 1 小时
- funding：每 15 分钟

#### Gold 层构建

```text
Silver candles + Silver funding
  → funding_aligner（as-of join：每根 bar 继承最新已发布 funding rate）
  → replay_bar_builder（构建 Gold bars，upsert 写入 gold 表）
```

### 21.4 覆盖范围（Phase 1 冻结）

| 维度 | 范围 |
|------|------|
| 交易所 | OKX |
| Spot | BTC-USDT, ETH-USDT |
| Swap | BTC-USDT-SWAP, ETH-USDT-SWAP |
| Timeframe | 1m, 5m, 15m, 1H |
| 数据域 | candles (OHLCV) + funding rates |

### 21.5 质量验证与门控

#### Candle 质量检查

| 检查项 | 级别 |
|--------|------|
| 重复 (symbol, ts) | `fail` — 阻断 merge |
| OHLC 非法 (h < l, 价格 <= 0) | `fail` |
| 时间乱序 | `fail` |
| 缺失间隔 | `warn` — 继续但记录 |
| Volume 负值 | `warn` |
| 可疑 bar (h < o 等) | `warn` |

#### Funding 质量检查

| 检查项 | 级别 |
|--------|------|
| 重复 (symbol, ts) | `fail` |
| 时间乱序 | `fail` |
| funding_rate 为 null | `warn` |

质量报告写入 `meta.quality_reports`，每次验证有完整记录。

### 21.6 可观测性

| 机制 | 存储位置 | 说明 |
|------|----------|------|
| 运行追踪 | `meta.ingest_runs` + `meta.ingest_run_items` | 全链路 run_id 可审计 |
| 文件状态 | `meta.raw_source_files` | pending / ingested / skipped / failed |
| 质量报告 | `meta.quality_reports` | 每次验证的详细指标 |
| Checkpoint | `meta.ingest_checkpoints` | 持久化水位线，gap 检测 |
| Scheduler 防重 | 进程内 bucket dedup | 单进程 best-effort，重启可能重跑一次 |

### 21.7 Timeframe 路由规则

OKX candle 文件名本身不携带 timeframe。路由优先级：

1. CLI `--timeframe` 显式指定（最高优先）
2. 目录名推断（如 `downloads/1m/BTC-USDT-candlesticks-2026-04-01.zip`）
3. 以上都没有 → 显式标记为 `skipped`，原因写入 `meta.raw_source_files.parse_error`

允许的目录名：`1m`, `5m`, `15m`, `1H`（以及别名 `1h`, `4h`, `4H`, `1d`, `1D`）。

### 21.8 配置

所有配置通过 `.env.research` 读取，前缀 `RDP_`。

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `RDP_DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg://localhost:5432/aats_research` |
| `RDP_HISTORICAL_DOWNLOAD_DIR` | 历史文件下载根目录 | `./data/historical` |
| `RDP_OKX_REST_URL` | OKX REST API 地址 | `https://www.okx.com` |
| `RDP_OKX_TIMEOUT_SECONDS` | API 超时 | `15` |
| `RDP_OKX_RATE_LIMIT_SLEEP` | API 请求间隔 | `0.12` |
| `RDP_ROLLING_CANDLES_ENABLED` | 滚动 candles 开关 | `true` |
| `RDP_ROLLING_CANDLES_SYMBOLS` | 采集 symbol 列表 | `BTC-USDT,ETH-USDT,BTC-USDT-SWAP,ETH-USDT-SWAP` |
| `RDP_ROLLING_CANDLES_TIMEFRAMES` | 采集 timeframe 列表 | `1m,5m,15m,1H` |
| `RDP_ROLLING_FUNDING_ENABLED` | 滚动 funding 开关 | `true` |
| `RDP_ROLLING_FUNDING_SYMBOLS` | funding symbol 列表 | `BTC-USDT-SWAP,ETH-USDT-SWAP` |
| `RDP_GOLD_REPLAY_BUILD_ENABLED` | Gold 层构建开关 | `true` |

模板文件：`configs/templates/.env.research.example`

### 21.9 数据库与迁移

数仓使用独立数据库 `aats_research`，与交易系统分库。

```powershell
# 创建数据库
psql -U postgres -c "CREATE DATABASE aats_research"

# 方式 A：通过 Python 执行迁移
python -c "from aats.data_platform.db import run_migrations; run_migrations()"

# 方式 B：手动逐个执行
psql -U postgres -d aats_research -f migrations/research/0001_create_research_schemas.sql
psql -U postgres -d aats_research -f migrations/research/0002_create_meta_tables.sql
# ... 依次到 0011
```

迁移文件清单（`migrations/research/`）：

| 文件 | 内容 |
|------|------|
| `0001` | 创建 meta / staging / bronze / silver / gold 五个 schema |
| `0002` | meta 表：dataset_manifests, raw_source_files, ingest_runs, ingest_run_items, ingest_checkpoints, quality_reports |
| `0003` | staging candle 表 (8 张: spot/swap x 1m/5m/15m/1h) |
| `0004` | staging funding 表 |
| `0005` | bronze candle 表 (8 张) |
| `0006` | bronze funding 表 |
| `0007` | silver candle 表 (8 张) |
| `0008` | silver funding 表 |
| `0009` | gold replay bar 表 (8 张) |
| `0010` | 全表 updated_at 触发器 + 补充索引 |
| `0011` | raw_source_files 增加 `skipped` 状态 |

### 21.10 快速开始

```powershell
# 1. 准备 .env.research
cp configs/templates/.env.research.example .env.research
# 编辑 .env.research，填入真实数据库连接串

# 2. 创建数据库并执行迁移
python -c "
from sqlalchemy import create_engine, text
engine = create_engine('postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/postgres', isolation_level='AUTOCOMMIT')
with engine.connect() as conn:
    conn.execute(text('CREATE DATABASE aats_research'))
"
python -c "from aats.data_platform.db import run_migrations; run_migrations()"

# 3. 历史回填（以 1m 为例）
python scripts/rdp_run_backfill.py --dir ./data/historical --timeframe 1m

# 4. 运行验收
python scripts/rdp_phase1_acceptance.py
```

### 21.11 代码模块清单

| 文件 | 职责 |
|------|------|
| `config.py` | Pydantic 配置，从 `.env.research` 加载 `RDP_` 前缀环境变量 |
| `db.py` | 连接池管理 (pool_size=5, max_overflow=10) + migration runner |
| `models.py` | CandleRow / FundingRow / ReplayBarRow 数据类 + 表名解析器 |
| `collectors/backfill/file_discovery.py` | ZIP 文件扫描、SHA256 去重、meta 注册、目录 timeframe 推断 |
| `collectors/backfill/file_parser.py` | OKX CSV/ZIP 解析、header 标准化 (BOM/引号/空格容错) |
| `collectors/backfill/candles_backfill_collector.py` | candle 历史回填编排 + timeframe 路由决策 |
| `collectors/backfill/funding_backfill_collector.py` | funding 历史回填编排 |
| `collectors/rolling/candles_api_collector.py` | OKX REST API candle 增量采集 + 去重 + checkpoint |
| `collectors/rolling/funding_api_collector.py` | OKX REST API funding 增量采集 + 去重 + checkpoint |
| `normalize/symbol_mapper.py` | canonical symbol 映射（Phase 1: 4 个 instrument） |
| `normalize/time_normalizer.py` | ms epoch -> UTC datetime 转换 |
| `validate/candle_quality_checker.py` | candle 质量检查（重复/缺失/乱序/OHLC/volume） |
| `validate/funding_quality_checker.py` | funding 质量检查（重复/乱序/null rate） |
| `validate/report_writer.py` | 质量报告写入 `meta.quality_reports` |
| `merge/bronze_merger.py` | staging -> bronze upsert |
| `merge/silver_merger.py` | bronze -> silver upsert |
| `merge/merge_pipeline.py` | 端到端编排：validate -> bronze -> silver + 质量门控 |
| `gold/funding_aligner.py` | as-of join：funding rate 对齐到 candle bar |
| `gold/replay_bar_builder.py` | Gold replay bar 构建 + upsert |
| `jobs/scheduler.py` | 滚动采集调度器 (cadence boundary + bucket 防重) |
| `jobs/checkpoint_manager.py` | checkpoint 水位线管理 (get/upsert/advance) |
| `jobs/run_registry.py` | ingest_run / run_item 生命周期管理 |
| `jobs/gap_repair.py` | Silver 层 gap 检测 + repair run 创建 |

### 21.12 已知限制（Phase 1）

| 项目 | 说明 |
|------|------|
| Scheduler 防重 | 进程内 in-memory bucket dedup，重启后可能重跑一次（下游 upsert 幂等） |
| Symbol 白名单 | 硬编码 4 个 instrument，后续需改为数据库驱动 |
| Gold volume 语义 | spot vol = 基础币量，swap vol = 合约张数，未做跨类型统一 |
| 单进程 | scheduler 设计为单进程，不支持多 worker 并行 |

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

