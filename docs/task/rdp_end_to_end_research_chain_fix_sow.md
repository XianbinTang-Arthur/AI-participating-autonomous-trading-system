# RDP 端到端研究链修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 业务目标与边界

- 目标：修复 RDP 从 Phase 2 研究、Phase 5 治理、Phase 6 决策到受控发布链中的证据丢失、入口失效、契约分叉和静默兜底问题。
- 目标：让 `Run Research` / workflow / full pipeline 的结果准确反映系统状态，避免“研究已完成但证据为空”或“workflow success 但实际未执行”。
- 边界：不移除人工审批，不把生产参数发布改成自动化直写。
- 边界：不发明未经验证的 directional 生产参数语义映射；对无法安全映射的内容采用 fail-closed 或显式诊断。

## 2. 模块职责与领域模型

- Step 2 研究：汇总 formal scan 与 calibration 结果，产出 `scan_comparison_summary.json` 与 `parameter_candidates.json`。
- Phase 5 治理：构建 artifact index / active round index / quality monitor，对研究产物建立可信索引。
- Phase 6 决策：基于 Phase 2/3/4/5 证据构建 combo 级评估，生成 recommendation / active decision / readiness。
- 发布链：approved recommendation -> gate -> release -> apply -> active parameter sets。

## 3. 输入 / 输出接口

- 输入：
  - `comparison_summary.json`（scan runner）
  - `parameter_candidates.json` / `parameter_candidates_merged.json`
  - `artifact_index.json` / `active_round_index.json`
  - workflow JSON (`configs/rdp_workflows/*.json`)
- 输出：
  - `scan_comparison_summary.json`
  - `evidence_summary.json`
  - `parameter_upgrade_candidates.json`
  - `family_timeframe_decisions.json`
  - `promotion_readiness_report.json`
  - workflow run report

## 4. 数据库模式 / 表 / 约束

- 仅读写既有治理表：
  - `governance.parameter_sets`
  - `governance.recommendations`
  - `governance.active_decisions`
  - `governance.active_parameter_sets`
  - `governance.decision_round_snapshots`
- 本次不新增表结构，不修改现有列定义。

## 5. 事务、一致性、并发

- registry / decision snapshot 仍沿用现有 DB-first + 文件 fallback 设计。
- 不新增跨表长事务；只修复读写路径和索引口径。
- 参数导入对 placeholder / `None` / 缺关键字段采用跳过或报错，避免把不完整参数写入治理状态。

## 6. 授权、认证与数据安全

- 保持现有 `approved recommendation -> gate -> release/apply` 安全边界。
- 不读取或展示任何凭证文件内容。
- 不降低生产 apply 保护等级。

## 7. 错误处理与幂等性

- workflow 任务在 `returncode=0` 但未满足成功标记时应判定为失败。
- Phase 2 / Phase 6 证据缺失时不再静默按 0 使用，而要返回 combo 级 `evidence_unavailable`/空统计。
- 参数导入遇到 placeholder / `None` / 非 dict 值时跳过，并记录原因。

## 8. 状态迁移与生命周期

- Parameter set 生命周期保持：`draft -> candidate -> frozen -> deprecated`。
- 研究链修复后，full pipeline 结束状态明确为“研究/治理建议已生成”，不暗示 live 已生效。
- 生产参数生效仍必须经过 release/apply 流。

## 9. 缓存与性能

- 复用现有 artifact index / active round index。
- 新增 combo 级 Phase 2 聚合时仅对已加载的 comparison / diagnostics 做内存汇总，不引入额外重扫描。

## 10. 日志、监控、审计

- workflow dispatcher 记录 success marker 缺失。
- Step2 / Phase6 / parameter import 对跳过原因和契约不一致做 warning/error 级日志。
- 保持 decision round snapshot 和 release/audit 日志链完整。

## 11. 测试策略

- 补 unit tests：
  - Phase 2 comparison schema 归一化
  - Phase 6 combo 级聚合 / 评分
  - workflow success marker 校验
  - parameter import 过滤 placeholder / `None`
  - full pipeline 参数文件选择 / 时间窗 / dataset version 行为
- 运行：
  - `ruff check aats/ --fix`
  - `pytest tests/unit/ -x -q`
  - 受影响最窄 integration test（WSL2）

## 12. 迁移、回滚、兼容性

- 对 `comparison` / `rows` / `experiments` 三种 legacy schema 做兼容读。
- workflow dispatcher 新字段采用可选配置，不破坏现有 workflow。
- artifact root 同时兼容旧 `calibration_rounds` 与新 `step2_rounds/step3_rounds`。

## 13. 配置与环境隔离

- 统一 dataset version canonical 值为 `v1.0`，旧 `v1` 作为兼容 alias。
- full pipeline 顶层 `--start/--end/--lookback-days` 明确向 Phase 2 / Step3 透传。
- 生产 apply 仍受环境守卫控制。

## 14. 代码组织与依赖

- 尽量将 schema normalization / combo key / import sanitization 作为小型 helper 放入现有模块中，避免新增复杂层次。
- 避免改动主交易策略逻辑；仅修复 RDP 证据、治理和 orchestration。

## 15. 文档与操作手册

- 更新相关运行文档或内联注释，反映 `schedule_hint` 为建议值、full pipeline 结束后仍需治理/发布。
- 保持 `docs/operations/parameter_governance.md` 的人工审批语义。

## 16. 部署与验收标准

- 验收 1：Step2 能正确汇总 `comparison`，不再出现 scan 已跑但 experiments=0。
- 验收 2：Phase6 对不同 combo 使用各自 Phase 2 证据，不再全局复用。
- 验收 3：`decision_cycle` workflow 实际运行 Phase 6；错误入口不再静默成功。
- 验收 4：full pipeline 明确停在“待治理/待发布”，不会伪装成 live 已更新。
- 验收 5：导入链不再接受 placeholder / `None` / 无开仓候选。
