# Route A Evidence Bundle Scaffold SoW

> 项目定位声明：本任务默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 `docs/project_positioning.md`。

## 1. 业务目标与边界

### 目标

为未来**真正出现的** Route A（microstructure directional）candidate 提供最小自动化打包能力，把已有:

- backtest evidence scorecard JSON
- observation-window JSON summary
- evidence 模板

拼成一份**可审计、可复现、可 review** 的 evidence bundle scaffold。

### 明确边界

- **不**发明 candidate
- **不**输出 verdict / go-no-go / archive 判断
- **不**改 live path / runtime mode / configs
- **不**扩展 carry / 15m directional / shadow candidate
- **不**引入 DB 写入、消息总线、副作用型后台任务

## 2. 模块职责与领域模型

### 新增能力

一个纯本地 CLI / helper，负责：

1. 读取已有 `scorecard.json`
2. 读取已有 observation-window JSON summary
3. 创建 `docs/research/route_a_phase0/<proposal_id>/` scaffold
4. 写入 bundle manifest / provenance metadata
5. 复制或落地 evidence 模板骨架

### 领域对象

- `proposal_id`
- `feature`
- `horizon`
- `scorecard_json`
- `observation_window_json`
- `bundle_manifest`

## 3. 输入 / 输出接口

### 输入

- `proposal-id`
- `feature`
- `horizon`
- `scorecard-json` 路径
- `observation-window-json` 路径
- 可选 `proposer`
- 可选 `output-root`（默认 `docs/research/route_a_phase0`）

### 输出

在 `docs/research/route_a_phase0/<proposal_id>/` 下生成最小 bundle：

- `manifest.json`
- `scorecard.json`（复制）
- `observation_window_summary.json`（复制）
- `proposal.md`（由模板预填充元数据和已知路径）

## 4. 数据库 / Schema / 约束

- 不涉及数据库 schema 变更
- 不新增表 / 索引 / migration

## 5. 事务 / 一致性 / 并发

- 纯文件系统本地操作
- 写文件采用“先建目录、再逐文件写入”顺序
- 已存在同名 `proposal_id` 默认拒绝覆盖，避免静默污染审计轨迹

## 6. 鉴权 / 安全 / 数据保护

- 不读取 `.env.*` 原文
- 不输出密码、token、凭证
- 只处理 repo 内已有 research artifact

## 7. 错误处理 / 幂等

- 缺少输入文件 → 明确报错退出
- 输入 JSON 缺少关键顶层字段 → 明确报错退出
- 输出目录已存在 → 明确报错退出
- 成功创建后再次运行同一 `proposal_id` 应失败，而不是覆盖

## 8. 状态迁移与生命周期

- `不存在 bundle` → `bundle scaffold created`
- 不引入后续自动状态；只提供 evidence 初始打包骨架

## 9. 缓存 / 性能

- 无缓存要求
- 文件量极小，性能不是瓶颈

## 10. 日志 / 监控 / 审计

- CLI stdout 可打印生成路径摘要
- `manifest.json` 必须包含:
  - `proposal_id`
  - `feature`
  - `horizon`
  - `generated_at`
  - `source_paths`
  - `source_sha256`

## 11. 测试策略

至少覆盖：

1. 正常 scaffold 创建
2. 复制 scorecard / observation JSON
3. `proposal.md` 含预填充元数据
4. 输入 JSON 缺关键键时报错
5. 输出目录已存在时报错

## 12. 迁移 / 回滚 / 兼容

- 无 migration
- 仅新增 CLI / helper，不改既有 backtest public behavior

## 13. 配置与环境隔离

- 仅依赖本地文件路径
- 不依赖 WSL2 / Docker / Postgres

## 14. 代码组织与依赖

优先放在现有 research/backtest CLI 邻近位置，避免新建复杂层级。

建议候选：

- `aats/cli.py`
- `aats/data_platform/replay/backtest/` 下新增轻量 helper
- `tests/unit/` 下新增对应 CLI / helper 测试

## 15. 文档与运维

- 本 SoW 即任务边界
- 如 CLI 面向开发者可复用，可补最小 docstring / help text

## 16. 部署与验收标准

### 不需要 deploy

该任务是 research / governance 工具链增强，不触 live runtime。

### 验收标准

- 能用一条 CLI 命令生成 route A evidence bundle scaffold
- 生成物路径、元数据、输入 provenance 清晰
- 单元测试通过
- 不改 live 行为
