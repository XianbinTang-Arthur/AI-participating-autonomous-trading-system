# Task 33: Legacy 清理计划

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 目标

阶段 1 到阶段 7 已经把系统主语义切到以下四个对象：

- `baseline_reference`
- `ai_decision_intent`
- `decision_outcome`
- `profile_control_decision`

当前 legacy 面主要还剩下两类：

1. `ai_takeover_*` 相关字段、事件、接口、测试
2. 一些仍围绕 takeover 叙事组织的 operator / UI / replay 兼容口径

本计划的目标不是立刻删除所有 legacy，而是明确：

- 哪些旧字段必须只读保留
- 哪些旧事件 / topic 还需要过渡保留
- 哪些页面入口、接口字段、测试用例可以在下一轮直接清掉

---

## 2. 总体原则

### 2.1 主链唯一主口径

主链、query、UI、operator API 后续统一围绕：

- `baseline_reference`
- `ai_decision_intent`
- `decision_outcome`
- `profile_control_decision`

任何 `ai_takeover_*` 都只能属于：

- legacy 兼容字段
- 历史审计材料
- 回放兼容引用

不能继续作为主链新逻辑的输入或主展示字段。

### 2.2 Legacy 只读，不再扩展

保留下来的 legacy 字段或 topic：

- 只能读
- 不能继续承载新语义
- 不再继续扩展字段定义

### 2.3 删除顺序

删除顺序必须是：

1. 先从主渲染和主 query 口径移出
2. 再从接口字段中降级成 legacy
3. 再从测试里移出主断言
4. 最后才删事件和持久引用

---

## 3. 旧字段只读保留清单

这些字段建议进入 `legacy read-only` 状态，不再作为主字段扩展，但短期保留。

### 3.1 `PositionTarget` 上的 legacy takeover 字段

位置：
- [decision.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\schemas\decision.py)

字段：
- `ai_takeover_allowed`
- `ai_takeover_applied`
- `ai_takeover_blockers`

保留原因：
- 单元测试和部分 replay / audit 仍依赖这些字段
- 当前 orchestrator 还会产出 legacy takeover 事件

状态建议：
- `只读保留`
- 不再作为新主链判断依据

退出条件：
- `target_position.py` 不再用这组字段来构造任何对外主语义
- `AI_TAKEOVER_DECISIONS` 事件彻底下线
- replay / audit 完成迁移

### 3.2 `DecisionAuditRecord` 上的 legacy 引用

位置：
- [audit.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\schemas\audit.py)

字段：
- `ai_takeover_decision_ref`

保留原因：
- 当前审计记录和 replay 校验仍直接引用这个 event ref

状态建议：
- `只读保留`

退出条件：
- audit 迁移到新的 `decision_outcome_ref` 或等价主语义引用
- replay 不再要求 `AI_TAKEOVER_DECISIONS`

### 3.3 Query 层 legacy 输出字段

位置：
- [runtime_queries.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\runtime_queries.py)
- [query_service.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\query_service.py)

字段：
- `legacy_takeover`
- `legacy_takeover_summary`
- `legacy_ai_takeover_decision`

保留原因：
- 兼容旧接口消费方
- 历史 operator 审计视图仍可读取

状态建议：
- `只读保留`
- 所有新前端和新接口文档都必须明确标注为 legacy

退出条件：
- 没有前端主渲染依赖
- 没有 API 客户端依赖
- 测试只保留历史兼容覆盖或完全删除

---

## 4. 旧事件 / topic 过渡保留计划

### 4.1 需要暂时保留的 legacy topic

#### `topics.AI_TAKEOVER_DECISIONS`

位置：
- [topics.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\events\topics.py)

当前生产者：
- [orchestrator.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\decision_engine\orchestrator.py)

当前消费者：
- [audit.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\decision_engine\audit.py)
- [query_service.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\query_service.py)
- [runtime_queries.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\runtime_queries.py)
- [replay.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\reconciliation_service\replay.py)

建议状态：
- `过渡保留`

建议保留到：
- 完成一次完整的 replay / audit 迁移
- 至少一轮稳定版本后再下线

建议下线条件：
1. `DecisionOutcome` 成为 replay 的主对照来源
2. `DecisionAuditRecord` 不再依赖 `ai_takeover_decision_ref`
3. `/ai/takeovers/recent` 已正式移除
4. 历史审计页不再展示 legacy takeover event

### 4.2 不建议继续新增任何 takeover topic 派生事件

说明：
- `AI_TAKEOVER_DECISIONS` 已经足够作为过渡兼容材料
- 不应再新增：
  - `AI_TAKEOVER_SUMMARY`
  - `AI_TAKEOVER_AUDIT_V2`
  - 任何新 takeover 主题

原则：
- 要迁移就迁到 `DecisionOutcome`
- 不要继续给 legacy 做新投资

---

## 5. 下一轮可直接删除的页面入口和字段

### 5.1 页面主渲染已不再需要的旧字段

前端主视图现在已经不再依赖 takeover 做主判断，因此下一轮可直接从主 view model 中移除这些读取路径：

位置：
- [ai-view.js](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\modules\views\ai-view.js)

可删除项：
- `overview.latest_takeover`
- `latest.takeover`
- takeover 作为 `latest` 主卡片 fallback 的逻辑

可保留项：
- 历史区的 `legacy takeover 审计`

### 5.2 `/ai/takeovers/recent` 页面入口

位置：
- [routes.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\routes.py)
- [store.js](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\modules\store.js)
- [app.js](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\app.js)

当前用途：
- 只给历史区提供 legacy takeover 审计列表

建议：
- 下一轮直接移除页面级入口
- 如果确实还要保留历史材料，改成只在 audit detail 里可见，不再做单独 AI 页面入口

### 5.3 前端历史区 takeover 统计卡

位置：
- [ai-view.js](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\modules\views\ai-view.js)

可删除的 UI 区块：
- “遗留接管允许率”
- “遗留接管应用率”
- “遗留 takeover 审计”表格和分页按钮

保留条件：
- 只有当产品仍明确要求保留 takeover 历史审计面板时才留

否则建议：
- 下一轮直接整块删掉

---

## 6. 下一轮可以彻底删掉的测试

### 6.1 Operator API 中围绕 takeover 主字段的断言

位置：
- [test_operator_api.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\tests\integration\test_operator_api.py)

下一轮可删除的类型：
- 对 `/ai/takeovers/recent` 的主功能断言
- 对 `legacy_takeover_summary` 的主功能断言
- 对 `legacy_ai_takeover_decision` 的主路径断言

替代方向：
- 改测：
  - `baseline_reference`
  - `ai_decision_intent`
  - `decision_outcome`
  - `profile_control_decision`

### 6.2 `test_target_position_engine.py` 中面向 takeover 字段的断言

位置：
- [test_target_position_engine.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\tests\unit\test_target_position_engine.py)

当前仍存在的 legacy 断言：
- `target.ai_takeover_allowed`
- `target.ai_takeover_applied`
- `target.ai_takeover_blockers`

下一轮建议：
- 全部替换为：
  - `decision_outcome.decision_source`
  - `decision_outcome.decision_blocked_reasons`
  - `decision_outcome.decision_authority`

### 6.3 `test_ai_inference.py` 中依赖 takeover blocker 名字的断言

位置：
- [test_ai_inference.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\tests\unit\test_ai_inference.py)

当前情况：
- 这些测试实际上在验证 AI 决策门禁
- 但使用的是 `ai_takeover_blockers` 命名

下一轮建议：
- 改成围绕 `DecisionOutcome.decision_blocked_reasons`
- 或围绕 `AIDecisionIntent` + gate decision 的新命名

### 6.4 Persistence / replay 中对 `ai_takeover_decision_ref` 的校验

位置：
- [test_persistence_and_replay.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\tests\integration\test_persistence_and_replay.py)

建议：
- 不要立即删
- 这是最后一批删，因为它与事件持久化和 replay 强相关

---

## 7. 推荐的清理顺序

### 第一步：清 UI 与主 query 口径

目标：
- takeover 不再出现在任何主页面主卡片
- `/ai/latest`、`/ai/overview` 主口径只保留新字段

动作：
1. 删除前端 takeover 主视图残余 fallback
2. 删除 `latest_takeover` / `takeover_summary` 旧别名
3. 文档明确 legacy 字段名

### 第二步：清接口入口

目标：
- `/ai/takeovers/recent` 不再作为常规页面数据入口

动作：
1. 从 [store.js](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\modules\store.js) 去掉 `aiTakeoversRecent`
2. 从 [app.js](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\app.js) 去掉 takeover 分页动作
3. 从 [routes.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\routes.py) 删除 `/ai/takeovers/recent`

### 第三步：清测试

目标：
- 测试只围绕新四对象表达主语义

动作：
1. 替换 `test_target_position_engine.py` 的 takeover 断言
2. 替换 `test_ai_inference.py` 的 takeover blocker 断言
3. 删除 operator API 中 takeover 主断言

### 第四步：清事件与 replay

目标：
- `AI_TAKEOVER_DECISIONS` 不再需要

动作：
1. replay 迁到 `DecisionOutcome`
2. audit 迁到新的主链 outcome 引用
3. 删除：
   - `AI_TAKEOVER_DECISIONS`
   - `ai_takeover_decision_ref`
   - `AITakeoverDecision` 兼容产出链

---

## 8. 具体保留时限建议

### 建议长期只读保留，直到 replay 迁移完成

- `topics.AI_TAKEOVER_DECISIONS`
- `DecisionAuditRecord.ai_takeover_decision_ref`
- `PositionTarget.ai_takeover_allowed`
- `PositionTarget.ai_takeover_applied`
- `PositionTarget.ai_takeover_blockers`

### 建议下一轮就清掉

- `latest_takeover`
- `takeover_summary`
- `/ai/takeovers/recent`
- `aiTakeoversRecent` 前端 store 入口
- AI 页面上的 takeover 历史主面板

### 建议在下一轮替换后删除

- `legacy_takeover`
- `legacy_takeover_summary`
- `legacy_ai_takeover_decision`

## 9. AI_TAKEOVER_DECISIONS 与 ai_takeover_decision_ref 最终退场路径

### 第 1 步：退出主查询与主页面

目标：
- `/ai/overview`
- `/ai/latest`
- `/decision/{id}`

不再把 takeover 兼容字段作为主返回的一部分。

结果：
- `legacy_takeover`
- `legacy_takeover_summary`
- `legacy_ai_takeover_decision`

只允许保留在审计明细的 legacy 区或内部兼容层。

### 第 2 步：退出主测试断言

目标：
- 单元测试和集成测试不再以 `ai_takeover_*` 作为主语义断言。

替换方向：
- `DecisionOutcome.decision_source`
- `DecisionOutcome.decision_authority`
- `DecisionOutcome.decision_blocked_reasons`
- `ProfileControlDecision`

说明：
- 到这一步，takeover 只剩 replay / audit 兼容价值。

### 第 3 步：audit 引用迁移

目标：
- `DecisionAuditRecord.ai_takeover_decision_ref` 不再作为核心审计引用。

迁移动作：
1. 在 audit / replay 里改为围绕 `position_target_ref` 与原生 `decision_outcome` 回放。
2. `audit_detail` 的 legacy 链接只保留只读兼容窗口。
3. 所有新的 audit 写入不再依赖 `ai_takeover_decision_ref`。

完成标志：
- replay 不再校验 `AI_TAKEOVER_DECISIONS`
- persistence 测试不再要求 `ai_takeover_decision_ref`

### 第 4 步：停止生产 legacy event

目标：
- `DecisionOrchestrator` 不再发布 `topics.AI_TAKEOVER_DECISIONS`

影响范围：
- [orchestrator.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\decision_engine\orchestrator.py)
- [config.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\bootstrap\config.py)
- [audit.py](D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\decision_engine\audit.py)

要求：
1. 审计服务先切到 `DecisionOutcome`
2. replay 先切到 `position_target_ref` / `decision_outcome`
3. operator 不再读取 takeover event

### 第 5 步：删除 schema / topic / ref

最后一批删除：
- `topics.AI_TAKEOVER_DECISIONS`
- `AITakeoverDecision`
- `DecisionAuditRecord.ai_takeover_decision_ref`
- audit service 中 takeover 专用 handler
- replay 中 takeover 专用校验路径

退出标准：
- 主链、query、UI、测试、replay、audit 全部不再读取 takeover 兼容语义

前提：
- 对外兼容窗口结束
- 没有外部调用方依赖

---

## 9. 下一轮执行建议

如果按最稳的顺序推进，我建议下一轮直接做：

1. 删除 AI 页面 takeover 历史面板及相关 store/action  
2. 删除 `/ai/takeovers/recent` 接口  
3. 把单元测试从 `ai_takeover_*` 迁到 `decision_outcome`  
4. 单独开一批做 replay / audit 的 legacy 事件迁移  

这样可以把风险拆开：

- 前三项是 UI / API / 单测层的清理
- 最后一项才是事件和持久化层的重清理

---

## 10. 当前结论

当前系统已经完成：

- 主链语义迁移
- query 主口径迁移
- 前端主渲染迁移

剩下的 legacy takeover 相关内容，已经不再是主功能依赖，属于：

- 历史兼容
- replay / audit 兼容
- 部分旧测试遗留

因此下一轮可以正式开始删除，而不再只是“继续弱化”。
