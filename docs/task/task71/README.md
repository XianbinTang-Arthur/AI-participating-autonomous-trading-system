# Task71: 运行模式 / 策略档位 / Shadow 解耦重构

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 背景

当前系统把几件不同的事情绑得太紧：

- AI 是否参与最终交易决策
- 策略档位是否允许自动切换
- 是否启用策略层 shadow
- 是否启用执行层 shadow

这会让前端、配置和运行时语义都变得模糊。尤其是：

- `AI 决策者并控制策略档位` 同时承载了“AI 最终决策”和“自动切档”两层含义
- `恢复自动切档` 在很多其实不会触发自动切档的场景里仍然可见、可点

## 目标

把“自动换档”做成**独立功能、独立控制**，不再默认和任何 AI 运行模式耦合。

## 设计原则

1. 自动换档必须有独立开关
2. AI 运行模式不能再隐式决定自动换档是否启用
3. UI 约束必须和后端真实逻辑一致
4. 先做兼容式拆分，不一次性大改所有历史链路

## 本轮实施

### Phase 1

- 新增独立配置：
  - `AATS_STRATEGY_PROFILE_AUTO_CONTROL_ENABLED`
- 后端改造：
  - `DecisionOrchestrator` 不再通过 `ai_decision_maker_with_profile_control` 隐式启用自动换档
  - 改成只看独立开关
- 运行时状态暴露：
  - `strategy_profile_auto_control_configured`
  - `strategy_profile_auto_control_effective`
  - `strategy_profile_auto_control_reason`
- 前端收口：
  - 在 AI 配置页里，非自动换档启用状态时，`恢复自动切档` 灰掉
  - 文案明确说明“当前不是自动换档模式”

### Phase 2

- 允许 `baseline_only + 自动换档`
- 允许 `ai_assisted + 自动换档`
- 彻底把“运行模式”和“自动换档”拆成两个独立控制卡

### Phase 3

- 把 `strategy shadow` 和 `execution shadow` 在配置和 UI 上正式拆名
- 重写相关帮助文案和 operator 说明

## 验收标准

- 自动换档总开关关闭时：
  - 后端不再自动评估主链档位控制
  - `恢复自动切档` 按钮不可点击
  - 后端接口 `/strategy-profiles/restore-auto` 返回明确错误
- 自动换档总开关开启时：
  - 不论当前 AI 运行模式是什么，只要主链允许，就可以跑自动换档逻辑

## 本轮落点

- [settings.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py)
- [orchestrator.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/orchestrator.py)
- [runtime_queries.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/runtime_queries.py)
- [query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py)
- [auth_routes.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/auth_routes.py)
- [ai-config-view.js](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/static/modules/views/ai-config-view.js)
