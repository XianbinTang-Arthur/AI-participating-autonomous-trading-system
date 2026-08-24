# Task 34：Mode 兼容层收缩与阈值命名迁移

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标

继续压缩旧 AI mode 和旧阈值命名在系统中的可见性，同时保持运行时兼容，不破坏现有部署。

## 迁移结论

本阶段采用“两层迁移”：

1. 配置输入层继续兼容旧命名
2. 代码读取层统一切到 canonical 命名

## 阈值命名映射

旧字段：

- `ai_primary_min_confidence`
- `ai_primary_max_uncertainty`
- `ai_primary_min_directional_edge`

canonical 字段：

- `ai_decision_min_confidence`
- `ai_decision_max_uncertainty`
- `ai_decision_min_directional_edge`

对应环境变量：

- `AATS_AI_DECISION_MIN_CONFIDENCE`
- `AATS_AI_DECISION_MAX_UNCERTAINTY`
- `AATS_AI_DECISION_MIN_DIRECTIONAL_EDGE`

## 当前实施策略

- `AATSSettings` 以 canonical 字段为正式字段名
- `AATSSettings` 继续兼容旧字段输入：
  - `ai_primary_min_confidence`
  - `ai_primary_max_uncertainty`
  - `ai_primary_min_directional_edge`
- 运行时代码统一读取 canonical 字段
- 当时根目录新增 `.env.example`，只展示 canonical 环境变量（该历史文件当前已不存在，不应把它当作现行配置模板）
- [configs/base.yaml](D:\文件\project\AIParticipatingAutonomousTradingSystem\configs\base.yaml) 已切换到 canonical 键名

## 旧 mode 可见性策略

- `ai_runtime()` 对外主字段：
  - `configured_operating_mode`
  - `effective_operating_mode`
  统一返回 canonical mode
- 旧 mode 仅下沉到：
  - `legacy_modes.configured_operating_mode`
  - `legacy_modes.effective_operating_mode`

## 后续收口顺序

### 阶段 A

- 所有运行时代码只读取 canonical 字段
- 测试逐步从旧阈值输入迁移到 canonical 输入

### 阶段 B

- 所有配置示例、配置文档、运维文档统一使用 canonical 名称
- 旧字段只保留兼容说明，不再作为推荐写法

### 阶段 C

- 当外部配置和测试全部完成迁移后
- 再考虑移除旧字段兼容输入

## 不变量

- 不改变实际交易门槛的数值语义
- 不破坏现有旧 `.env` 的启动兼容能力
- UI 和 operator API 默认只围绕 canonical mode 叙事
