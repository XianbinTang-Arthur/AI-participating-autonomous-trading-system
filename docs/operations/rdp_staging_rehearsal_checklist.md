# RDP Staging 准生产演练清单

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 目的：在 `RDP_ENV=staging` 下完整验证 recommendation → gate → release → observation → rollback 链路，再允许进入 `prod` 试运行。

---

## 1. 环境准备

- [ ] `RDP_ENV=staging`
- [ ] staging 使用独立的 governance DB / live DB 只读连接 / artifacts 目录
- [ ] `RDP_PRODUCTION_APPLY_ENABLED` 在 staging 环境保持显式配置
- [ ] `python scripts/rdp_run_reliability_check.py` 返回 0
- [ ] `/rdp/health` 显示 `overall_health` 为 `healthy` 或可接受的 `degraded`

## 2. Workflow 演练

- [ ] `data_maintenance` 成功执行
- [ ] `governance_cycle` 成功执行
- [ ] `research_cycle` 成功执行
- [ ] `decision_cycle` 成功执行
- [ ] `artifacts/operations/workflow_runs/` 中可看到上述 4 个 workflow 的最新成功报告

## 3. Recommendation 到 Release

- [ ] 生成至少 1 条 `approved` recommendation
- [ ] `POST /rdp/gates/run` 返回 `pass` 或可接受的 `warn`
- [ ] `POST /rdp/releases/create` 成功创建 release
- [ ] release 记录包含 `gate_result_ref / gate_status / previous_parameter_set_id / observation_window_hours`
- [ ] staging 下 `skip_gate=true` 被拒绝

## 4. Observation 与 Rollback

- [ ] `python scripts/rdp_run_release_observation_cycle.py` 能处理 observing releases
- [ ] `artifacts/production_workflow/observations/<release_id>/` 已生成 observation 结果
- [ ] `artifacts/metrics/release_effectiveness_registry.json` 已生成 effectiveness 评估
- [ ] 至少完成一次 rollback recommendation 评估
- [ ] 至少完成一次 rollback 演练，并确认 active parameter set 恢复

## 5. 准入门槛

- [ ] prod 下 direct apply 被拒绝
- [ ] prod 下 `skip_gate=true` 被拒绝
- [ ] prod 下观察窗口 < 72h 被拒绝
- [ ] `/rdp/health` 能在 daemon 心跳丢失、live DB 不健康、workflow 过期时正确降级
- [ ] 相关 operator / runbook 文档已与当前实现一致

## 6. 演练记录

- [ ] 演练日期:
- [ ] 执行人:
- [ ] recommendation_id:
- [ ] gate_run_id:
- [ ] release_id:
- [ ] rollback 演练结果:
- [ ] 是否允许进入 prod 试运行:
