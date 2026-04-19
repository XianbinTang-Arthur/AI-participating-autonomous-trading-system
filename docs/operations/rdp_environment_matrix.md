# RDP 环境矩阵

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 环境概览

| 维度 | dev | staging | prod |
|------|-----|---------|------|
| `RDP_ENV` | `dev` (默认) | `staging` | `prod` |
| 用途 | 开发、调试 | 集成测试、验证 | 正式运行 |
| 数据源 | 本地 / 测试 DB | 预发布 DB | 生产 DB |
| 谁使用 | 开发人员 | 开发 + 运维 | 运维人员 |

## 操作权限矩阵

| 操作 | dev | staging | prod |
|------|-----|---------|------|
| 参数 Apply | ✅ 无限制 | ✅ 需 gate | ✅ 需 gate + 审批 |
| 参数 Rollback | ✅ 无限制 | ✅ 无限制 | ✅ 无限制 |
| Workflow 执行 | ✅ 无限制 | ✅ 无限制 | ✅ 无限制 |
| 直接 DB 访问 | ✅ 允许 | ✅ 允许 | ❌ 禁止 |
| API 读取 | ✅ | ✅ | ✅ |
| API 写入 | ✅ | ✅ | ✅ (需认证) |

## 观察窗口矩阵

| 环境 | Apply 后观察时长 | 说明 |
|------|----------------|------|
| dev | 0h | 无观察期，即时生效 |
| staging | 24h | 最少观察 24 小时 |
| prod | 72h | 最少观察 72 小时 |

## 审批流程矩阵

| 步骤 | dev | staging | prod |
|------|-----|---------|------|
| Recommendation 生成 | ✅ | ✅ | ✅ |
| Recommendation 审批 | 可跳过 | 可跳过 | **必须** |
| Pre-Apply Gate | 可跳过 | **必须** | **必须** |
| Parameter Apply | 直接 | Gate 通过后 | Gate + 审批通过后 |
| Post-Apply Observation | 可跳过 | 24h | 72h |
| Rollback Evaluation | 可跳过 | 可选 | **必须** |

## 数据流隔离

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│   dev    │     │ staging  │     │   prod   │
├──────────┤     ├──────────┤     ├──────────┤
│ 本地 DB  │     │ 预发布DB │     │ 生产 DB  │
│ 本地文件 │     │ 预发布文件│     │ 生产文件 │
│          │     │          │     │          │
│ artifacts│     │ artifacts│     │ artifacts│
│ /dev/    │     │ /staging/│     │ /prod/   │
└──────────┘     └──────────┘     └──────────┘
      ↑                ↑                ↑
   开发人员          测试验证         运维操作
```

## 配置差异

### 环境变量

| 变量 | dev | staging | prod |
|------|-----|---------|------|
| `RDP_ENV` | `dev` | `staging` | `prod` |
| `DATABASE_URL` | localhost:5432/rdp_dev | staging-host:5432/rdp_staging | prod-host:5432/rdp_prod |
| `RDP_API_PORT` | 8080 | 8080 | 8080 |
| `RDP_LOG_LEVEL` | DEBUG | INFO | WARNING |

### Workflow 配置

所有环境共享相同的 workflow JSON 配置文件。差异通过 `environment_guard.py` 策略层实现。

## 部署路径

```
dev → staging → prod

1. 在 dev 开发和测试
2. 部署到 staging 进行集成验证
3. staging 验证通过后部署到 prod
```

### 部署检查清单

- [ ] 代码变更通过 dev 测试
- [ ] `RDP_ENV=staging` 下运行所有 workflow (dry-run)
- [ ] staging 可靠性检查通过
- [ ] staging 中完整执行一次 decision_cycle
- [ ] 按 `rdp_staging_rehearsal_checklist.md` 完成 recommendation → gate → release → observation → rollback 演练
- [ ] 确认 `RDP_ENV=prod` 已设置
- [ ] prod 可靠性检查通过
- [ ] 通知运维人员

## 回退策略

| 场景 | dev | staging | prod |
|------|-----|---------|------|
| 代码回退 | git revert | git revert | git revert + 通知 |
| 参数回退 | 直接 rollback | rollback + 记录 | rollback + 审批 + 观察 |
| 配置回退 | 直接修改 | 修改 + 验证 | 修改 + 验证 + 通知 |

## 监控矩阵

| 监控项 | dev | staging | prod |
|--------|-----|---------|------|
| Workflow 执行状态 | 手动检查 | 自动检查 | 自动检查 + 告警 |
| 可靠性检查 | 按需 | 每日 | 每日 + 即时告警 |
| 告警通知 | 控制台 | 控制台 + 日志 | 控制台 + 日志 + 外部通知 |
| 失败记录 | 可选 | 必须 | 必须 + 及时响应 |
