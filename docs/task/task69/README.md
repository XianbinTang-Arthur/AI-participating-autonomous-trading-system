# Task69: AI 跳档逻辑重构

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 背景

当前策略档位自动切换存在两个直接问题：

1. 样本不足阶段会过度偏向保守档位，尤其容易把系统推到 `execution_degraded_safe`。
2. 安全降级档与常规盈利档共用同一套 winner competition，导致“安全保护”与“盈利优化”混在一起。

这会带来两个后果：

- 新启动或证据不足时，系统容易自动收缩到极保守状态，抑制本应允许的趋势入场。
- 档位控制目标从“在可控风险下最大化净收益质量”偏移成“样本不足时默认不做”。

## 重构目标

本次重构不以“多出单”为目标，而以“更合理地服务盈利验证”为目标。

需要达成以下结果：

1. 样本不足时，档位系统默认保持基线档或当前档，不因证据缺失而自动偏向安全档。
2. `execution_degraded_safe` 仅在明确安全触发下参与自动切换。
3. 自动切档必须满足最低证据门槛，而不是一次候选胜出就能切换。
4. 冷启动阶段进入显式锁定，防止 AI 在缺乏样本时频繁改档。
5. Operator / 前端需要能看到：
   - 当前是否处于冷启动锁定
   - 当前是否存在安全档触发
   - 当前自动切档还缺什么证据

## 设计原则

### 1. 盈利档与安全档分流

- 盈利档：`trend_normal`、`trend_strict`、`trend_aggressive`、`range_defensive`、`high_volatility_defensive`
- 安全档：`execution_degraded_safe`

安全档不再参加常规盈利竞争，只能由明确安全事件触发。

### 2. 样本不足中性化

`replay_history_insufficient` 不再自动给保守档加分，也不再给趋势档减分。

证据不足时，replay 对所有候选档的贡献应为中性。

### 3. 冷启动锁定

在达到最小实盘样本和最小 replay 校验数量前：

- 不自动切换到新的盈利档
- 允许继续保持当前档
- 允许在明确安全事件下切入安全档

### 4. 自动切档证据门槛

自动切档新增以下硬门槛：

- 最小 closed trades
- 最小 replay validations
- 冷启动锁定未解除时阻止非安全切档

## 任务拆解

### 子任务 1: 设置层与策略控制摘要

- 新增 profile control 相关设置
- 新增冷启动/安全触发/证据状态摘要
- 输出到 strategy profile snapshot

### 子任务 2: replay 证据中性化

- 修正 replay scorecard 在 `validation_count = 0` 时的正负偏置
- 明确记录“证据不足，中性处理”

### 子任务 3: 安全档从常规竞争中剥离

- 为候选档增加 `selection_eligible` 与 `selection_blocked_reasons`
- `execution_degraded_safe` 只有在安全触发时才 eligible

### 子任务 4: 自动切档 gate 强化

- 冷启动锁定阻止非安全自动切档
- 未达到最小 trade/replay 证据时阻止自动切档
- 在 selection decision 和 API 中暴露阻断原因

### 子任务 5: 前端与运维可见性

- strategy 页新增“档位控制”卡片
- 清晰展示：
  - 当前 active profile
  - 是否处于冷启动锁定
  - 是否触发安全档条件
  - 自动切档还缺什么证据

### 子任务 6: 回归测试

- 单元测试：
  - replay 证据不足时中性处理
  - 安全档无触发时不可自动推荐
  - 冷启动锁定会阻止非安全切档
- 集成测试：
  - strategy profile API 暴露新的控制摘要
  - dashboard UI 展示新的中文说明

## 验收标准

1. 在 replay 验证数量为 0 时，趋势档不再因 replay 被自动罚分，安全档也不再因 replay 被自动加分。
2. 没有明确安全事件时，`execution_degraded_safe` 不再成为自动切档候选赢家。
3. 在 closed trades / replay validations 不足时，系统会返回明确的冷启动或证据不足阻断原因。
4. 前端 strategy 页可以直接看到当前档位控制状态与阻断原因。
5. 现有 strategy profile 自动切档相关回归继续通过。
