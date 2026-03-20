# Task 35: Blocker Control Plane 重构设计

## 1. 文档目标

本文档定义交易系统的阻断控制面重构方案。

当前系统已经具备较完整的阻断检测能力，但仍存在以下核心问题：

- 阻断原因数量多，来源分散，页面只显示表层状态，无法稳定指向根因
- 同一时刻存在多条阻断时，没有明确的优先级和处理顺序
- 大量阻断只有说明，没有对应操作入口，用户无法在系统内完成闭环处理
- 风险页、AI 工作台、对账详情、策略档位页分别暴露了零散动作，缺少统一控制面
- 阻断状态与操作状态之间没有形成明确状态机，容易出现“知道卡住了，但不知道怎么解开”的死锁体验

本次重构的目标不是再加几个按钮，而是将现有阻断体系升级为完整的 **Blocker Control Plane**：

- 明确主阻断原因
- 对多条阻断做优先级排序
- 为每条阻断提供可执行的解决方式
- 形成统一的阻断控制面板
- 把“检测、解释、操作、反馈、继续下一步”做成闭环

---

## 2. 设计目标

### 2.1 用户目标

当系统不能继续自动交易时，用户必须能在一个地方直接回答这 5 个问题：

1. 系统现在为什么不能继续自动交易
2. 这些原因里哪一个最先处理
3. 当前还有哪些次级阻断
4. 每条阻断应该怎么处理
5. 处理完这一条之后，下一步应该做什么

### 2.2 工程目标

阻断系统必须满足以下工程要求：

- 同一份阻断数据能同时服务：
  - 风险与恢复页
  - AI 工作台
  - 账户/对账详情
  - 运维与审计查询
- 阻断原因不能只靠字符串拼接，而要有结构化定义
- 所有用户可点击动作都必须具备：
  - 权限约束
  - 幂等性
  - 审计记录
  - 明确的预期效果
- 主阻断原因必须来自明确优先级算法，而不是数组第一个元素
- 失败后的系统状态必须可重算，不能依赖前端缓存“猜当前状态”

### 2.3 非目标

本次设计不覆盖以下内容：

- 重写 execution engine
- 重写 reconciliation comparator 逻辑
- 引入复杂 BPM/审批引擎
- 将所有内部技术性 blocker 全量暴露为首页主信息
- 在本轮里把所有阻断全部变成自动修复

---

## 3. 问题定义

### 3.1 当前问题

当前系统中的阻断大致分成四层：

1. 系统运行阻断
2. 提交限制阻断
3. AI 决策阻断
4. 策略档位自动切换阻断

问题不在于“没有 blocker code”，而在于：

- 没有统一聚合
- 没有统一优先级
- 没有统一动作模型
- 没有统一展示与处理流程

### 3.2 当前典型失败路径

以本轮实际问题为例：

1. 对账要求人工确认基线
2. 用户完成基线确认
3. AI shadow 触发人工复核
4. `kill_switch_active` 作为表层状态覆盖了真正根因
5. `resume` 因 `resume_eligible = false` 被灰掉
6. 页面没有 AI 复核按钮，系统进入死锁

这说明当前设计的问题是：

- 真实根因没有被正确上浮
- 阻断没有形成“处理动作”
- 处理动作没有形成“下一步状态”

---

## 4. 阻断模型总览

## 4.1 阻断分层

所有阻断统一分为四类：

### A. `system_execution`

定义：直接影响系统是否允许继续自动交易。

示例：

- `reconciliation_halt_required`
- `operator_rebaseline_required`
- `ai_degraded_requires_manual_review`
- `account_snapshot_missing`
- `account_state_stale`
- `market_connection_down`
- `market_data_stale`
- `rebaseline_in_progress`
- `kill_switch_active`

### B. `submission_mode`

定义：系统允许运行，但不会真实向交易所提交订单。

示例：

- `guarded_execution_dry_run`
- `live_submit_disabled`
- `local_demo_no_exchange_submission`
- `real_market_paper_uses_local_paper_execution`
- `real_money_live_not_supported`
- `guarded_live_blocked_by_default`
- `paper_execution_has_no_exchange_submission`

### C. `ai_decision`

定义：AI 本轮不能主导交易决策，但系统不一定整体停机。

示例：

- `ai_confidence_below_threshold`
- `ai_uncertainty_above_threshold`
- `ai_directional_edge_too_small`
- `ai_override_not_recommended`
- `ai_not_economically_actionable`
- `ai_output_invalid`
- `ai_fallback_used`
- `ai_post_close_cooldown_active`
- `ai_low_edge_cooldown_active`
- `ai_execution_performance_guard_active`

### D. `profile_control`

定义：AI 或系统不能自动切换策略档位，但不一定影响主交易链继续运行。

示例：

- `strategy_profile_open_orders_present`
- `strategy_profile_switch_cooldown_active`
- `strategy_profile_auto_switch_disabled`
- `strategy_profile_auto_switch_confidence_too_low`
- `strategy_profile_manual_approval_required`
- `strategy_profile_auto_switch_frozen`
- `strategy_profile_candidate_requires_more_confirmations`
- `strategy_profile_min_active_duration_not_reached`
- `strategy_profile_score_delta_below_threshold`
- `strategy_profile_reconciliation_not_clean`

---

## 5. 主阻断与次级阻断

## 5.1 主阻断定义

主阻断不是“第一个 blocker”，而是：

> 当前所有阻断中，最先应该被用户处理，且处理后最能推动系统继续前进的那一条。

定义字段：

- `primary_blocker`

它必须满足：

- 唯一
- 可解释
- 可操作
- 可审计

## 5.2 次级阻断定义

其余阻断以优先级排序，作为：

- `secondary_blockers`

用于回答：

- 处理完主阻断后，还剩哪些阻断
- 哪些只是表层状态
- 哪些只是提交限制，不阻断恢复

## 5.3 优先级原则

优先级要体现“根因优先于表象、人工决策优先于状态位、系统安全优先于便利”。

建议优先级从高到低如下：

1. `reconciliation_halt_required`
2. `operator_rebaseline_required`
3. `ai_degraded_requires_manual_review`
4. `account_snapshot_missing`
5. `account_state_stale`
6. `market_connection_down`
7. `market_data_stale`
8. `rebaseline_in_progress`
9. `kill_switch_active`
10. 提交限制类
11. AI 决策级阻断
12. profile control 阻断

注意：

- `kill_switch_active` 不是根因优先级最高的 blocker
- 它通常只是“系统已暂停”的表面状态
- 只有在没有更高优先级根因时，才应成为主阻断

## 5.4 计算规则

建议新增统一排序函数：

- `rank_blocker(blocker_code: str) -> int`

然后基于统一规则得到：

- `sorted_blockers`
- `primary_blocker = sorted_blockers[0]`
- `secondary_blockers = sorted_blockers[1:]`

---

## 6. 统一 Schema 设计

## 6.1 BlockerAction

每条阻断必须带动作，不再只返回文案。

```python
class BlockerAction(BaseModel):
    action_id: str
    label: str
    kind: Literal["primary", "secondary", "danger", "link", "noop"]
    enabled: bool
    requires_confirmation: bool = False
    confirmation_title: str | None = None
    confirmation_body: str | None = None
    disabled_reason: str | None = None
    api_target: str | None = None
    method: Literal["POST", "GET"] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_effect: str | None = None
    next_state_hint: str | None = None
```

说明：

- `noop` 用于不可在系统内处理的 blocker
- `link` 用于跳转查看详情
- `expected_effect` 用于前端提示“执行后会发生什么”
- `next_state_hint` 用于提示“处理完这一条后请继续做什么”

## 6.2 BlockerResolution

每条 blocker 的完整结构：

```python
class BlockerResolution(BaseModel):
    blocker: str
    category: Literal["system_execution", "submission_mode", "ai_decision", "profile_control"]
    priority: int
    severity: Literal["critical", "high", "medium", "low", "info"]
    subsystem: str
    affects_execution: bool
    submit_only: bool = False
    review_required: bool = False
    auto_recoverable: bool = False
    title: str
    description: str
    impact: str
    recommended_action: str
    actions: list[BlockerAction] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
```

说明：

- `title`：短标题，适合卡片标题
- `description`：解释为什么发生
- `impact`：说明会影响什么
- `recommended_action`：一句话概括最建议处理方式
- `evidence`：放可用于 UI 展示的关键上下文，如 reconciliation id、ai report id、snapshot ts

## 6.3 BlockerControlPanel

统一面板聚合结构：

```python
class BlockerControlPanel(BaseModel):
    trading_state: Literal["tradable", "manually_halted", "blocked", "review_required", "submit_limited"]
    resume_eligible: bool
    safe_to_trade: bool
    halted: bool
    primary_blocker: BlockerResolution | None
    secondary_blockers: list[BlockerResolution] = Field(default_factory=list)
    pending_action_count: int = 0
    completed_action_hint: str | None = None
    next_recommended_step: str | None = None
```

---

## 7. 阻断动作设计

## 7.1 动作类型

所有动作统一归入四类：

### 1. 直接修复

点击后立即改变系统状态。

示例：

- 重新对账
- 确认为新基线
- 恢复自动运行
- 暂停自动运行

### 2. 人工裁决

需要用户做有后果的判断。

示例：

- 确认恢复 AI 决策
- 改为仅基础策略继续运行
- 继续保持暂停

### 3. 跳转检查

不能直接修复，但系统要给明确入口。

示例：

- 查看最新对账
- 查看 AI 复核详情
- 查看账户快照
- 查看行情状态

### 4. 不可在系统内处理

必须明确说明“为什么不能处理”，而不是给无效按钮。

示例：

- `real_money_live_not_supported`
- `local_demo_no_exchange_submission`

---

## 8. 第一批必须闭环的 blocker

第一批只做最核心、最容易把系统卡死的 blocker。

## 8.1 `ai_degraded_requires_manual_review`

### 语义

AI 因 shadow 表现或结果质量进入人工复核态，当前不允许直接恢复 AI 决策链路。

### 必须显示的信息

- AI 为什么进入复核
- 最近哪个窗口表现差
- 是 shadow underperformed、fee delta 过高，还是 churn 异常
- 当前是：
  - 仅待人工复核
  - 还是 provider 也已降级

### 必须提供的动作

1. `确认恢复 AI 决策`
2. `改为仅基础策略继续运行`
3. `继续保持暂停`
4. `查看 AI 复核详情`

### 正确语义

- “确认恢复 AI 决策”
  - 清除 outcome-review 型阻断
  - 不清除 provider 故障型阻断
  - 不直接下单

- “改为仅基础策略继续运行”
  - 将 AI 决策权降为 `baseline_only`
  - 清除该次 outcome-review 阻断
  - 使系统可恢复到 baseline-only 自动运行

## 8.2 `operator_rebaseline_required`

### 动作

1. `查看最新对账`
2. `重新对账`
3. `确认为新基线`

### 闭环要求

- 确认为新基线后，若无其他高优先级阻断，应自动推进到下一条 blocker

## 8.3 `reconciliation_halt_required`

### 动作

1. `查看最新对账`
2. `重新对账`
3. `继续保持暂停`

注意：

- 这类 blocker 不应提供“强行恢复”按钮

## 8.4 `market_data_stale`

### 动作

1. `查看行情状态`
2. `刷新行情状态`
3. `重新连接行情网关`（若实现）

### 自动恢复说明

- 这类 blocker 应标记为 `auto_recoverable = true`
- 如果行情恢复，应自动从阻断列表中消失

## 8.5 `account_snapshot_missing` / `account_state_stale`

### 动作

1. `查看账户快照`
2. `刷新账户状态`
3. `继续保持暂停`

## 8.6 `kill_switch_active`

### 动作

1. `恢复自动运行`
2. `继续保持暂停`

### 展示原则

- 只有在没有更高优先级根因时，才作为主阻断出现
- 否则应作为次级阻断显示为“当前系统已处于暂停态”

---

## 9. AI 复核状态机

这是当前系统最缺的闭环。

## 9.1 状态

建议将 AI 复核拆成明确状态：

- `none`
- `review_required`
- `review_approved_restore_ai`
- `review_rejected_degrade_to_baseline`
- `review_kept_halted`

## 9.2 事件

建议新增 operator action / event：

- `ai_review_restore_confirmed`
- `ai_review_degrade_to_baseline_confirmed`
- `ai_review_keep_halted`

## 9.3 行为

### 复核通过

- 清除 outcome review 阻断
- 恢复 AI 决策资格
- 若无其他 blocker，允许 `resume`

### 复核不通过

- 将系统切到 `baseline_only`
- 清除 outcome review 阻断
- 若无其他 blocker，允许 `resume`

### 继续暂停

- 保持人工暂停状态
- 但系统必须明确显示：
  - 为什么继续暂停
  - 以后可以做哪两个动作

---

## 10. 后端接口设计

## 10.1 新增聚合接口

### `GET /system/blocker-control`

返回统一阻断控制面数据：

```json
{
  "trading_state": "review_required",
  "resume_eligible": false,
  "safe_to_trade": false,
  "halted": true,
  "primary_blocker": {...},
  "secondary_blockers": [...],
  "pending_action_count": 2,
  "completed_action_hint": null,
  "next_recommended_step": "请先处理 AI 人工复核，再决定是否恢复自动运行。"
}
```

该接口取代前端自己拼装：

- recovery
- blockers
- ai runtime
- reconciliation
- account health

## 10.2 动作接口

建议新增以下后端动作：

### AI 复核

- `POST /system/ai-review/restore`
- `POST /system/ai-review/degrade-to-baseline`
- `POST /system/ai-review/keep-halted`

### 市场/账户刷新

- `POST /system/market/refresh`
- `POST /system/account/refresh`

### 已有动作继续复用

- `POST /system/reconcile`
- `POST /system/rebaseline`
- `POST /system/resume`
- `POST /system/halt`

## 10.3 返回约定

所有动作接口统一返回：

```json
{
  "status": "accepted",
  "action": "ai_review_degrade_to_baseline",
  "result": {
    "state_changed": true,
    "resume_eligible": true,
    "next_recommended_step": "当前已切换为仅基础策略运行，可恢复自动运行。"
  },
  "blocker_control": {...}
}
```

这样前端不需要再发二次请求猜状态。

## 10.4 与现有后端接口的联动关系

Blocker Control Plane 不是独立的新子系统，它必须建立在现有接口和现有状态源之上。

建议明确区分：

- **直接复用的现有接口**
- **保留但降为底层数据源的接口**
- **必须新增的聚合接口**
- **必须新增的动作接口**

### 10.4.1 可直接复用的现有动作接口

以下接口已有明确业务语义，可直接纳入 blocker 动作模型：

- `POST /system/reconcile`
- `POST /system/rebaseline`
- `POST /system/resume`
- `POST /system/halt`

对应现有后端逻辑主要在：

- [reconciliation_system_queries.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/reconciliation_system_queries.py)
- [query_service.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py)
- [routes.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/routes.py)

### 10.4.2 可复用但不应直接暴露给前端主流程的接口

以下接口/查询仍然保留，但应作为 blocker control 的底层数据源，不建议由风险页直接拼装调用：

- `GET /system/blockers`
- `GET /system/recovery`
- `GET /ai/runtime`
- `GET /ai/performance/overview`
- `GET /reconciliation/latest`
- `GET /account/state`
- `GET /runtime-profiles/summary`
- `GET /strategy-profiles/summary`

原因：

- 它们是局部视图，不是阻断控制面的完整语义
- 直接由前端拼装这些接口，容易再次回到“多个面板、多个状态源、互相打架”的老问题

### 10.4.3 必须新增的聚合接口

本任务至少应新增：

- `GET /system/blocker-control`

该接口统一联动以下后端状态源：

- `health_service.snapshot()`
- `recovery_posture.assess()`
- `runtime.kill_switch`
- `ai_service.status()`
- `latest reconciliation report`
- `account service status()`
- `mode_controller.snapshot()`
- `strategy profile activation state`

### 10.4.4 必须新增的动作接口

以下接口目前系统没有完整闭环能力，必须新增：

- `POST /system/ai-review/restore`
- `POST /system/ai-review/degrade-to-baseline`
- `POST /system/ai-review/keep-halted`
- `POST /system/market/refresh`
- `POST /system/account/refresh`

如果后续决定把 profile control 也并入 blocker control，还需要预留：

- `POST /system/profile-control/freeze`
- `POST /system/profile-control/unfreeze`

但这两项不属于本轮第一批必须项。

## 10.5 Blocker 与现有接口的映射表

| blocker | 当前主要状态源 | 现有可复用动作 | 本轮需新增动作 |
|---|---|---|---|
| `operator_rebaseline_required` | `recovery_view`, `latest reconciliation` | `reconcile`, `rebaseline` | 否 |
| `reconciliation_halt_required` | `health snapshot`, `recovery_view` | `reconcile` | 否 |
| `rebaseline_in_progress` | `recovery_view` | 无直接动作 | 否 |
| `kill_switch_active` | `runtime.kill_switch` | `resume`, `halt` | 否 |
| `market_data_stale` | `market provider status` | 无统一刷新动作 | 是，`market/refresh` |
| `market_connection_down` | `market provider status` | 无统一刷新动作 | 是，`market/refresh` |
| `account_snapshot_missing` | `account_service.status()` | 无统一刷新动作 | 是，`account/refresh` |
| `account_state_stale` | `account_service.status()` | 无统一刷新动作 | 是，`account/refresh` |
| `ai_degraded_requires_manual_review` | `ai_service.status()`, `recovery_posture` | 无 | 是，三条 AI review 动作 |
| `submit-only` 阻断 | `mode_controller.snapshot()`, execution readiness | 通常无 | 一般不需要 |

---

## 11. 后端实现建议

## 11.0 模块化原则

阻断控制面不应继续散落在：

- `query_service.py`
- `recovery_posture.py`
- `risk-view.js`
- `ai-view.js`
- 若干零散 route

建议把 blocker control 抽成一个独立模块，而不是继续作为 operator query 的副产物。

推荐的新模块边界：

- `aats/services/blocker_control/`

建议至少拆成以下文件：

- `models.py`
  - 定义 `BlockerResolution`、`BlockerAction`、`BlockerControlPanel`
- `priority.py`
  - 定义 blocker 分类、优先级和主阻断选择逻辑
- `resolver.py`
  - 把底层状态翻译成统一 blocker 模型
- `actions.py`
  - 定义 blocker 动作的服务层执行逻辑
- `snapshot.py`
  - 统一生成 blocker control 快照
- `audit.py`
  - 负责 blocker action 审计和状态变更记录

### 为什么必须模块化

如果不单独抽模块，后面一定会继续出现这些问题：

- query 层既做聚合又做控制，职责混乱
- 路由层直接拼动作，难以统一权限和幂等
- 前端不同页面各自理解 blocker，语义漂移
- 数据库存储无法围绕 blocker 形成清晰模型

Blocker control 本质上已经是一个独立子系统，应该获得独立边界。

## 11.1 Query 层

建议在 [query_service.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py) 中新增：

- `blocker_control_panel()`
- `_rank_blocker(...)`
- `_resolve_blocker(...)`
- `_blocker_actions(...)`
- `_primary_blocker(...)`

同时保留现有：

- `recovery_view()`
- `blockers()`

作为底层数据源，但前端不再直接消费它们的原始形态。

## 11.2 AI 服务层

在 [inference.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/ai_service/inference.py) 中增加：

- outcome-review 明确确认接口
- outcome-review 拒绝并降为 `baseline_only` 接口
- provider fault 与 outcome-review fault 的分离恢复逻辑

关键原则：

- 人工复核不能清除 provider 自身故障
- 人工复核不能直接下单
- 人工复核只影响后续决策资格

### 11.2.1 当前缺失的后端函数

当前后端没有完整现成函数来处理以下异常：

#### A. AI outcome review 的人工确认

目前缺失：

- 明确的“复核通过，恢复 AI 决策”函数
- 明确的“复核不通过，降为 baseline_only”函数
- 明确的“继续保持暂停”函数

建议新增到 AI 服务或 operator action facade：

- `acknowledge_outcome_review_restore_ai(...)`
- `acknowledge_outcome_review_degrade_to_baseline(...)`
- `acknowledge_outcome_review_keep_halted(...)`

这些函数应负责：

- 校验当前是否真的处于 AI review required 状态
- 区分 outcome-review 型阻断与 provider 故障型阻断
- 更新 runtime 内 AI 决策资格
- 记录 operator action
- 触发 recovery status 重算

#### B. 行情刷新动作

当前缺失：

- 统一的 operator 级行情刷新动作

建议新增 facade 方法：

- `refresh_market_state_for_blocker_resolution(...)`

它应负责：

- 尝试刷新最新行情状态
- 返回刷新后的 freshness / connection 状态
- 不直接修改其他恢复状态

#### C. 账户快照刷新动作

当前缺失：

- 统一的 operator 级账户刷新动作

建议新增 facade 方法：

- `refresh_account_state_for_blocker_resolution(...)`

它应负责：

- 强制刷新账户快照
- 返回刷新后的 blockers
- 触发 recovery status 重算

### 11.2.2 当前后端虽有能力，但不适合直接暴露的函数

以下能力虽然存在，但不能直接当 blocker 动作 API：

- 直接调用 `account_service.refresh(force=True)`
- 直接调用行情网关内部刷新函数
- 直接改写 `kill_switch` 或 recovery status 内部字段
- 直接改写 AI service 内部状态位

原因：

- 缺少统一权限校验
- 缺少统一幂等语义
- 缺少统一 operator 审计
- 缺少统一返回结构

因此这些现有函数应只作为动作 facade 的底层调用，不应直接让路由层裸露出去。

## 11.3 Recovery / Query 层新增职责

建议在 [query_service.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py) 和相关 facade 中新增以下职责：

### 新增聚合函数

- `blocker_control_panel()`
- `resolve_primary_blocker()`
- `_primary_blocker_code()`
- `_blocker_resolution_model()`
- `_blocker_actions_for_code()`

### 新增动作函数

- `resolve_ai_review_restore()`
- `resolve_ai_review_degrade_to_baseline()`
- `resolve_ai_review_keep_halted()`
- `refresh_market_for_resolution()`
- `refresh_account_for_resolution()`

### 复用但要重新编排的函数

- `recovery_view()`
- `blockers()`
- `system_mode()`
- `account_state()`
- `ai_runtime()`
- `ai_performance_overview()`
- `latest_reconciliation()`

这些函数继续存在，但 blocker control 不应再让前端自己分散调用它们，而是在后端统一编排。

同时建议逐步把 blocker 聚合逻辑从：

- `OperatorQueryService._build_blockers()`
- `RecoveryPostureEvaluator.assess()`

迁出到 blocker control 模块，避免核心 blocker 语义散落两地。

## 11.4 路由层新增职责

建议在 [routes.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/routes.py) / [auth_routes.py](/abs/path/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/auth_routes.py) 中新增：

- `GET /system/blocker-control`
- `POST /system/ai-review/restore`
- `POST /system/ai-review/degrade-to-baseline`
- `POST /system/ai-review/keep-halted`
- `POST /system/market/refresh`
- `POST /system/account/refresh`

路由层要求：

- 统一写权限校验
- 统一错误码
- 统一返回 `blocker_control` 最新快照
- 统一 operator action 审计

## 11.5 审计

所有 blocker 动作都必须写 `OperatorActionRecord`：

- action
- actor
- auth_source
- previous_state
- resulting_state
- blocker_before
- blocker_after

这样排障时能复盘：

- 谁处理了什么 blocker
- 处理前后状态如何变化

---

## 16.1 数据库存储设计目标

建议 blocker control 不只依赖当前内存态和 event store 临时拼装，还应具备明确的持久化设计。

目标：

- 持久保存 blocker 快照
- 持久保存 blocker 动作
- 持久保存 blocker 生命周期
- 能支持历史查询、复盘、审计、统计

## 16.2 建议新增持久化对象

### A. `blocker_snapshots`

用途：

- 保存某个时间点系统全部 blocker 的结构化快照
- 支持后续对“当时为什么卡住”进行复盘

建议字段：

- `snapshot_id`
- `created_at`
- `runtime_state`
- `halted`
- `resume_eligible`
- `safe_to_trade`
- `primary_blocker_code`
- `primary_blocker_category`
- `primary_blocker_priority`
- `blockers_json`
- `source`

说明：

- 当前已有 `BlockerSnapshotRecord` 事件型快照，但建议明确数据库层结构，并使其与 blocker control 模块一致
- `blockers_json` 中可直接存完整 `BlockerResolution[]`

### B. `blocker_action_records`

用途：

- 记录每次用户或系统对 blocker 采取的动作

建议字段：

- `action_record_id`
- `created_at`
- `blocker_code`
- `action_id`
- `actor_role`
- `actor_identity`
- `auth_source`
- `status`
- `pre_state_json`
- `post_state_json`
- `result_summary`
- `error_code`
- `error_detail`

说明：

- 这张表可以和现有 `OperatorActionRecord` 并存，也可以最终收敛映射到 operator action 存储层
- 如果短期不想加新表，也至少应为 `OperatorActionRecord.details` 设计 blocker action 标准字段

### C. `blocker_resolution_sessions`

用途：

- 记录一次“从卡住到解开”的完整处理会话

建议字段：

- `session_id`
- `opened_at`
- `closed_at`
- `initial_primary_blocker`
- `final_state`
- `resolved`
- `action_count`
- `resolution_path_json`

说明：

- 不是第一阶段必须建
- 但对长期运维和产品可观测性很有价值

## 16.3 事件设计建议

建议新增统一 blocker 事件 topic，而不是继续只依赖 scattered event refs。

### 建议新增 topic

- `system.blocker_control_snapshots`
- `system.blocker_actions`
- `system.blocker_resolution_sessions`

### 事件语义

#### `system.blocker_control_snapshots`

记录：

- 当前 primary blocker
- 当前 blocker 列表
- 当前系统是否允许恢复

#### `system.blocker_actions`

记录：

- 用户点了什么动作
- 动作针对哪个 blocker
- 动作成功还是失败
- 状态如何变化

#### `system.blocker_resolution_sessions`

记录：

- 一组 blocker 从开始到解除的完整会话轨迹

## 16.4 数据库与事件的关系

建议采用：

- **数据库表负责查询与历史分析**
- **事件流负责实时审计与回放**

不要只依赖其中一种。

原因：

- 只靠 event store，做“当前 blocker 控制面”和历史统计都太重
- 只靠关系表，又会丢掉事件序列语义

推荐模式：

1. blocker control 模块在状态变化时发事件
2. 同时写 blocker snapshot / action record 持久层
3. query 层优先从快照层取当前结果，必要时回退 event store

## 16.5 第一阶段最小数据库方案

如果不想一口气引入三张新表，最小方案可以是：

1. 先保留现有 `BlockerSnapshotRecord`
2. 扩展其 payload 结构，使其承载：
   - `primary_blocker`
   - `secondary_blockers`
   - `actions`
3. 扩展 `OperatorActionRecord.details`
   - 统一写入 blocker action 标准字段

这样第一阶段不需要马上做 SQL migration，也能先把 blocker control 跑起来。

## 16.6 第二阶段推荐数据库方案

当 blocker control 稳定后，建议正式做 migration：

- 新建 `blocker_snapshots`
- 新建 `blocker_action_records`
- 视需要新建 `blocker_resolution_sessions`

并增加：

- 按 `created_at` 索引
- 按 `primary_blocker_code` 索引
- 按 `resolved / final_state` 索引

便于后续做：

- “哪类 blocker 最常出现”
- “哪类 blocker 最难处理”
- “平均解锁时长”
- “哪类 blocker 最常导致人工暂停”

---

## 12. 前端控制面板设计

## 12.1 总体原则

前端不再只显示说明文字，而是显示：

- 当前状态
- 主阻断
- 次级阻断
- 下一步动作

## 12.2 页面位置

建议将“阻断控制面板”放在：

- 风险与恢复页顶部主区块

同时在：

- AI 工作台

里保留与 AI 复核直接相关的局部动作卡片。

## 12.2.1 风险与恢复页“操作建议”区域重构

当前“风险与恢复”页面中已有一个“操作建议”区域，但它仍然是旧设计：

- 固定放置少量按钮
- 主要围绕对账和基线处理
- 不能根据真实阻断动态变化
- 当真正根因不是对账时，会出现“按钮和问题不匹配”的情况

本任务要求将该区域重构为：

> **阻断控制面板的第一优先级动作区**

也就是说：

- “操作建议”不再是一个写死的按钮集合
- 而是从统一 `BlockerControlPanel.primary_blocker` 中提取：
  - 当前第一优先级阻断
  - 该阻断的解释
  - 该阻断的主动作按钮
  - 处理后的下一步提示

### 设计原则

1. “操作建议”区域只服务 **当前第一优先级 blocker**
2. 如果存在多个 blocker：
   - 只在该区域展示第一优先级 blocker 的处理动作
   - 其余 blocker 放在“次级阻断列表”中
3. 当前 blocker 处理完成后：
   - 前端重新拉取 `GET /system/blocker-control`
   - 若主 blocker 变化，则“操作建议”区域切换为下一条 blocker 的动作区
4. 不再允许页面写死“重新对账 / 确认为新基线 / 恢复自动运行”这一类固定按钮组

### UI 结构建议

“操作建议”区域建议拆成四块：

- `当前优先处理`
  - 显示主 blocker 标题
- `为什么先处理它`
  - 显示该 blocker 的影响和与其他 blocker 的优先级关系
- `建议动作`
  - 显示 1 到 3 个主动作按钮
- `处理后下一步`
  - 告诉用户当前 blocker 解决后，系统会进入什么状态，或下一条 blocker 是什么

### 示例

#### 场景 A：`operator_rebaseline_required`

“操作建议”区域应显示：

- 当前优先处理：需要人工确认并重建基线
- 为什么先处理它：最新对账要求人工确认当前账实状态
- 建议动作：
  - 查看最新对账
  - 重新对账
  - 确认为新基线
- 处理后下一步：
  - 若无其他阻断，可继续恢复自动运行
  - 若仍有 AI 复核阻断，则自动切换到 AI 复核动作区

#### 场景 B：`ai_degraded_requires_manual_review`

“操作建议”区域应显示：

- 当前优先处理：需要完成 AI 人工复核
- 为什么先处理它：最近 shadow 连续弱于 baseline，当前不允许直接恢复 AI 决策链
- 建议动作：
  - 确认恢复 AI 决策
  - 改为仅基础策略继续运行
  - 继续保持暂停
- 处理后下一步：
  - 若改为仅基础策略继续运行，且无其他 blocker，则可恢复自动运行

### 前端组件建议

建议新增一个专用组件：

- `PrimaryBlockerActionPanel`

输入：

- `primary_blocker`
- `next_recommended_step`
- `pending_action_count`

职责：

- 专门渲染“操作建议”区域
- 与“次级阻断列表”解耦
- 不负责全量 blocker 管理，只负责当前要先处理哪一条

### 和“次级阻断列表”的关系

风险与恢复页建议采用以下结构：

1. 顶部状态摘要
2. **操作建议**：当前第一优先级 blocker 的动作区
3. 次级阻断列表
4. 状态依据 / 账户概览 / 对账概览 / 恢复概览

这样页面才符合真实处理顺序，而不是继续让用户在多个卡片之间自己猜先点哪个按钮。

## 12.3 面板结构

### 顶部状态条

显示：

- 当前交易状态
- 当前是否允许恢复
- 当前是否已暂停
- 当前主阻断数量

### 主阻断卡片

显示：

- 标题
- 原因说明
- 影响范围
- 下一步动作
- 主按钮组

### 次级阻断列表

每条显示：

- 名称
- 所属子系统
- 简要说明
- 推荐动作
- 单独按钮

### 最近操作反馈

显示：

- 最近一次处理动作
- 是否成功
- 系统是否还有剩余阻断

## 12.4 前端按钮原则

- 所有动作按钮都要显示：
  - 可不可点
  - 为什么不可点
  - 点击后会发生什么
- 对危险动作使用确认弹窗
- 对系统外不可处理的 blocker 显示只读说明，不给假按钮

---

## 13. 文案原则

## 13.1 文案必须回答三件事

每条 blocker 至少要回答：

1. 发生了什么
2. 为什么会影响交易
3. 下一步该做什么

## 13.2 禁止使用的表达

以下文案应避免直接作为主提示：

- “系统已暂停”
- “恢复受限”
- “resume blocked”
- “需要人工确认”

这些可以作为状态标签，但不能作为唯一主说明。

## 13.3 推荐格式

建议统一写成：

- 标题：当前需要先完成 AI 人工复核
- 原因：最近 shadow 连续弱于 baseline，系统已暂停 AI 决策链路
- 影响：在完成复核前，不允许恢复 AI 主导交易
- 下一步：请选择“确认恢复 AI 决策”或“改为仅基础策略继续运行”

---

## 14. 权限设计

## 14.1 角色要求

建议如下：

- 查看 blocker control：所有已登录 operator 可读
- 执行 blocker 动作：
  - `resume/halt/rebaseline/ai review` 需要 `admin`
  - 纯跳转查看动作不需要额外写权限

## 14.2 前端表现

无权限时：

- 按钮仍展示
- 但 disabled
- 明确提示：
  - “当前账号只有查看权限，不能执行该操作”

---

## 15. 幂等性与失败处理

## 15.1 幂等性要求

所有 blocker 动作都必须幂等。

例如：

- 重复点击“确认恢复 AI 决策”
  - 第二次不应报错
  - 应返回“当前已不在 AI 复核状态”

- 重复点击“改为仅基础策略继续运行”
  - 若已在 `baseline_only`
  - 应返回“当前已是 baseline_only”

## 15.2 部分失败

动作成功改变状态后，若 UI 刷新失败：

- 后端状态仍应正确
- 前端重试拉取 `/system/blocker-control` 即可恢复

## 15.3 自动恢复类 blocker

例如：

- `market_data_stale`
- `account_state_stale`

动作失败时不应把系统带到更坏状态。  
这类 blocker 支持“查看状态”和“触发刷新”，但系统也应允许其自然恢复。

---

## 16. 审计与可观测性

## 16.1 必须记录的审计信息

每次 blocker 动作必须记录：

- action
- actor
- action timestamp
- blocker code
- pre-state
- post-state
- whether resume became eligible
- whether safe_to_trade changed

## 16.2 建议新增指标

- `blocker_primary_count_by_code`
- `blocker_action_attempts_total`
- `blocker_action_success_total`
- `blocker_action_failures_total`
- `time_to_clear_primary_blocker_seconds`

这些指标有助于判断：

- 哪类 blocker 最常出现
- 哪类 blocker 最难被解决
- 哪类 blocker UI/动作设计仍然不够好

---

## 17. 测试设计

## 17.1 后端单元测试

至少覆盖：

- blocker 排序
- primary blocker 选择
- secondary blocker 去重和排序
- 每条 blocker 的动作生成
- AI review 通过 / 不通过 / 保持暂停

## 17.2 API 集成测试

至少覆盖：

1. `ai_degraded_requires_manual_review`
   - 能看到两条明确动作
   - `restore_ai`
   - `degrade_to_baseline`

2. `operator_rebaseline_required`
   - 查看对账
   - 重建基线
   - blocker 消失

3. `kill_switch_active` 不是主 blocker
   - 在 AI review blocker 存在时，只作为次级阻断

4. 多 blocker 并存时的排序
   - reconciliation > ai review > kill switch

## 17.3 前端测试

至少覆盖：

- 风险页显示主阻断卡片
- 次级阻断列表按优先级排序
- AI review 状态下出现两个核心按钮
- 无权限时按钮禁用并有说明
- 点击动作后页面刷新为下一状态

---

## 18. 分阶段实施建议

## 阶段 1：后端 blocker 聚合重构

输出：

- `BlockerResolution`
- `BlockerControlPanel`
- `GET /system/blocker-control`

## 阶段 2：第一批系统级 blocker 动作闭环

输出：

- AI review 三分支动作
- rebaseline 动作整合
- resume/halt 整合
- 市场/账户刷新动作

## 阶段 3：风险页控制面板

输出：

- 主阻断卡片
- 次级阻断列表
- 每条 blocker 按钮
- 最近处理结果

## 阶段 4：AI 工作台联动

输出：

- AI review 详情区
- AI 复核动作按钮
- 跳回风险页或恢复状态联动

## 阶段 5：提交级与 profile control blocker 纳入统一框架

输出：

- 提交限制类 blocker 的统一展示
- profile control 阻断的次级展示区

---

## 19. 风险与注意事项

### 19.1 不要让 blocker 面板变成“万能控制台”

原则：

- 只放真正与阻断处理有关的动作
- 不要把普通诊断按钮全塞进来

### 19.2 不要把内部技术 blocker 全铺给用户

原则：

- 主页面只显示最有操作意义的 blocker
- 其余 blocker 放折叠详情

### 19.3 不要把“状态位”误当成“根因”

最典型的就是：

- `kill_switch_active`

它通常只是当前状态，不是根因。

### 19.4 不要把 AI 复核做成单纯解锁按钮

AI 复核必须是：

- 恢复 AI 决策
- 或降为 baseline_only
- 或继续暂停

不能只是“点一下 resume 变亮”。

---

## 20. 本任务的正式范围

本任务建议正式定义为：

1. 重构 blocker 聚合逻辑，输出主阻断、次级阻断和结构化动作
2. 建立统一 blocker control panel 后端接口
3. 为第一批系统级 blocker 提供完整动作闭环
4. 在风险与恢复页落地统一阻断控制面板
5. 在 AI 工作台补齐 AI review 处理入口
6. 补齐权限、审计、幂等性、前后端测试

---

## 21. 一句话结论

当前系统最大的问题不是“不会检测异常”，而是：

> **系统知道自己为什么卡住，但没有把“先处理什么、怎么处理、处理完去哪一步”做成统一闭环。**

Task 35 的目标就是把这一点补齐，让阻断系统从“诊断提示”升级为“可操作控制面”。 

---

## 22. 当前设计的漏洞与不足

下面这些点如果不提前补进设计，后面即使把界面和按钮做出来，系统还是会继续出现“看起来能用，实际上会卡住或解释错误”的问题。

## 22.1 主阻断只是排序结果，还不是“可证明的根因”

当前设计里 `primary_blocker` 是通过优先级排序选出来的。  
这能解决“谁排第一”的问题，但还不能完全解决“它为什么是根因”的问题。

### 漏洞

- 两个 blocker 可能同时存在因果关系
- 例如：
  - `kill_switch_active`
  - `ai_degraded_requires_manual_review`
- 现在我们说后者优先，但系统并没有显式存储：
  - `kill_switch_active` 是被哪个 blocker 间接触发的

### 改进建议

在 `BlockerResolution` 中新增：

- `upstream_causes: list[str]`
- `derived_from: list[str]`

示例：

- `kill_switch_active`
  - `derived_from = ["ai_degraded_requires_manual_review"]`

这样前端可以明确显示：

- 主阻断：AI 需要人工复核
- 当前状态：系统已暂停
- 暂停不是根因，而是保护措施

## 22.2 缺少 blocker 生命周期模型

当前设计有：

- blocker snapshot
- blocker action

但还没有明确定义 blocker 从出现到消失的生命周期。

### 漏洞

如果没有生命周期定义，就会出现：

- blocker 被处理了，但页面仍显示“处理中”
- blocker 条件已消失，但没有被自动关闭
- 同一个 blocker 重复出现，前后记录无法关联

### 改进建议

建议为 blocker 增加生命周期状态：

- `open`
- `acknowledged`
- `in_progress`
- `resolved`
- `auto_cleared`
- `superseded`

并增加稳定标识：

- `blocker_instance_id`

说明：

- `blocker` 是 blocker code
- `blocker_instance_id` 是这一次具体发生的 blocker 实例

这样才能区分：

- 上一次的 `market_data_stale`
- 这一次新的 `market_data_stale`

## 22.3 动作执行缺少“前置条件校验”

当前设计定义了动作，但还没有强调：

> 每个动作在执行前必须重新校验前置条件

### 漏洞

例如：

- 用户看到 `确认恢复 AI 决策`
- 但在点击前，系统状态已经变成 provider degraded
- 如果不重验，就会把本来不该恢复的状态错误恢复

### 改进建议

为每个 `BlockerAction` 增加：

- `preconditions`
- `conflicts_with`

并要求服务层在执行动作时：

1. 重新读取当前状态
2. 校验 blocker 仍存在
3. 校验动作前置条件仍成立
4. 若不成立，返回结构化 `action_conflict`

返回示例：

```json
{
  "status": "conflict",
  "detail": "blocker_state_changed",
  "blocker_control": {...}
}
```

## 22.4 缺少并发与竞态设计

当前系统已经存在多个页面、多个刷新请求和后台自动恢复逻辑。  
阻断控制面如果不考虑并发，很容易再制造新的死锁。

### 漏洞

典型竞态：

1. 风险页和 AI 工作台同时点“恢复 AI”
2. 一个页面点“改为 baseline_only”，另一个页面点“resume”
3. 系统自动恢复类 blocker 刚消失，用户同时触发人工动作

### 改进建议

在 blocker action 执行层引入：

- 乐观版本号 `version`
- 或 `etag`

每次前端提交动作时携带：

- `blocker_instance_id`
- `panel_version`

后端校验：

- 当前 blocker 是否还是同一实例
- 当前面板版本是否仍有效

否则返回：

- `409 conflict`

## 22.5 自动恢复类 blocker 与人工处理类 blocker 没有彻底分开

当前设计里已经有 `auto_recoverable`，但还不够。

### 漏洞

例如：

- `market_data_stale`
- `account_state_stale`

这类 blocker 可能自己恢复。  
如果前端继续把它当成人工 blocker 放在主动作区，用户体验会变差。

### 改进建议

增加 blocker 处理类型：

- `resolution_mode`
  - `manual_only`
  - `auto_only`
  - `manual_or_auto`
  - `external_only`

这样前端就能正确展示：

- 自动恢复中
- 可手动触发刷新
- 必须人工裁决
- 只能到系统外处理

## 22.6 外部系统 blocker 还没有独立处理策略

当前设计里把这些 blocker 也放进统一体系是对的，但还不够：

- `real_money_live_not_supported`
- `local_demo_no_exchange_submission`
- `okx_account_mode_incompatible_with_derivatives`
- `okx_system_status_incident`

### 漏洞

这些 blocker 不是“系统内点个按钮就能修”。  
如果还按普通 blocker 处理，会误导用户以为系统能自修。

### 改进建议

新增 blocker 处理类型：

- `external_dependency`

并要求这类 blocker 提供：

- 明确的人类说明
- 外部处理步骤
- 可选的文档链接 / 查看详情

但不提供伪动作按钮。

## 22.7 缺少“动作后的状态转移图”

现在文档里描述了动作，但没有把动作和状态转移关系写成严格状态机。

### 漏洞

没有状态机，后续很容易出现：

- 一个动作清掉 blocker A，却把系统带到非法状态
- UI 以为进入 `resume_eligible`，实际上还差一步
- AI review、rebaseline、kill switch 三者之间顺序混乱

### 改进建议

明确加入状态机图，至少覆盖：

- `review_required`
- `resume_blocked`
- `manually_halted`
- `normal_operation`
- `baseline_only_fallback_running`

并给出动作迁移规则：

- `ai_review_restore`
- `ai_review_degrade_to_baseline`
- `rebaseline_confirmed`
- `resume`
- `halt`

## 22.8 blocker 结果与实际可执行状态之间缺少终态校验

当前项目已经有过一次类似问题：

- 页面说可以恢复
- 实际执行链路仍不允许继续

### 漏洞

如果 blocker control 只依赖 query 聚合，而不校验真实 execution readiness，就可能再次出现：

- blocker 控制面说“已解决”
- 但 `execution_adapter.readiness()` 仍然不允许执行

### 改进建议

在 blocker action 执行完成后，统一做一次：

- `post_action_readiness_check`

检查：

- `resume_eligible`
- `safe_to_trade`
- `execution adapter readiness`
- `market/account freshness`

若结果与预期不一致，返回：

- `resolved_partially`
- 并重新给出新的主 blocker

## 22.9 缺少“恢复后首轮保护”设计

即使 blocker 被处理掉，系统也不一定应该立刻按完全正常模式继续跑。

### 漏洞

例如：

- AI review 不通过，切到 `baseline_only`
- resume 之后第一轮就可能触发新决策

但在某些场景下更合理的是：

- 先恢复运行
- 但首轮只观察 / 不开新仓 / 不允许反手

### 改进建议

为 blocker action 增加可选恢复策略：

- `post_resolution_guard`

示例：

- `observation_only_until_next_clean_cycle`
- `baseline_only_until_manual_restore`
- `no_new_entries_for_n_minutes`

这能让恢复更安全。

## 22.10 缺少跨页面一致性要求

现在我们计划在：

- 风险与恢复页
- AI 工作台

同时提供 blocker 操作入口。

### 漏洞

如果不定义清楚两个页面的职责，容易出现：

- 风险页能处理，AI 页显示不一致
- AI 页能复核，风险页按钮状态没更新
- 同一个 blocker 在两个页面文案不一样

### 改进建议

明确页面职责：

- 风险与恢复页：
  - 主阻断处理总入口
- AI 工作台：
  - 仅对 AI 相关 blocker 提供详情和联动处理

并要求两页都只消费同一个：

- `GET /system/blocker-control`

而不是各自拼装自己的 blocker 视图。

## 22.11 缺少权限差异下的完整 UX 设计

当前文档提到“无权限按钮置灰”，但还不够。

### 漏洞

实际场景中可能出现：

- 只读用户能看到 blocker，但完全不知道该找谁处理

### 改进建议

为无权限状态增加：

- `escalation_hint`

例如：

- “当前账号没有处理权限，请联系管理员执行 AI 复核”

这在运维场景里很重要。

## 22.12 缺少历史视角与统计视角的分离

当前设计里 blocker control 同时承担：

- 当前控制面
- 审计
- 历史统计

### 漏洞

如果不拆开，后面接口会越来越大。

### 改进建议

明确拆成三类接口：

1. 当前控制面
   - `GET /system/blocker-control`
2. 历史动作
   - `GET /system/blocker-actions/history`
3. 统计分析
   - `GET /system/blocker-stats`

不要让一个接口承担全部职责。

## 22.13 缺少 rollout 策略

这个 blocker control 会影响多个页面和多个动作入口，直接切换风险很大。

### 漏洞

如果一次性替换所有页面：

- 很可能出现旧按钮已删，新控制面还不完整

### 改进建议

增加 rollout 计划：

### 阶段 A

- 后端先提供 `GET /system/blocker-control`
- 前端先做只读面板

### 阶段 B

- 逐个接入第一批 blocker 动作

### 阶段 C

- 删除旧“操作建议”按钮区
- 删除 AI 页旧复核提示入口

### 阶段 D

- 接入历史统计与审计视图

---

## 23. 建议补充到正式设计的强制约束

为了避免 blocker control 后续再次失控，建议把下面这些写成正式约束：

1. 所有 blocker 必须结构化定义，禁止前端直接硬编码 blocker 语义
2. 所有 blocker 动作必须是幂等的
3. 所有 blocker 动作执行前必须重新校验前置条件
4. 所有 blocker 动作执行后必须返回最新 blocker control 快照
5. 所有 blocker 必须明确：
   - 是否人工处理
   - 是否自动恢复
   - 是否系统外处理
6. 风险页“操作建议”只展示第一优先级 blocker 的动作区
7. AI 工作台只能展示 AI 相关 blocker 的详情与联动，不再维护独立 blocker 状态源
8. `kill_switch_active` 默认不得作为主根因，除非没有更高优先级 blocker
9. blocker control 必须具备持久化和审计能力，不能只依赖运行时内存态
10. 第一批 blocker 闭环做完前，不得继续扩展更多零散页面按钮
