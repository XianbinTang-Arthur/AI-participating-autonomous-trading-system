# AI 配置当前档位真实性修复 SOW

## 业务目标与边界

修复 AI 配置页在策略档位已经初始化或已被管理员切换后，仍把“当前档位”显示为“待确认”的问题。本次只修正 dashboard 读取路径，不改变策略参数、自动换档决策、交易执行或风控语义。

## 当前行为

AI 配置页通过 `/dashboard/bundle` 读取 `aiConfigModel`。当该 panel 的 dashboard snapshot 缺失、失效或刚被 mutation 清理时，snapshot plane 会立即返回默认值 `{"ai": {}}` 并在后台刷新。bundle 把这个不完整默认值当作成功结果交给前端；前端读不到 `strategy_profile.activation` 和 `active_revision`，因此回退显示“待确认”。

策略档位本身并未缺失：初始化逻辑会在没有激活版本时激活 `trend_normal`；管理员手动切档后，激活状态也已持久化。

## 模块职责与领域模型

- `aats.api.auth_routes`：决定 dashboard bundle 中哪些 panel 可以读取快照，哪些控制面数据必须按请求读取权威状态。
- `OperatorQueryService.ai_config_summary_with_runtime`：组合运行模式和策略档位的权威 AI 配置摘要。
- 前端 AI 配置视图：继续按现有 `strategy_profile` 结构渲染，不改变展示协议。

## 输入与输出接口

保留 `/dashboard/bundle?panel=aiConfigModel&panel=aiRuntime` 的请求和响应结构。成功返回的 `aiConfigModel.data` 必须包含：

- `runtime_profile`
- `strategy_profile`
- `ai`

其中 `strategy_profile.activation.active_profile_id` 或 `strategy_profile.active_revision.profile_id` 应能表达真实当前档位。

## 数据库 Schema、表、索引与约束

不新增或修改数据库表、列、索引、migration。继续读取现有策略档位激活状态。

## 事务、一致性与并发

`aiConfigModel` 改为 bundle 请求时读取权威状态，不再接受该 panel 的 snapshot 默认占位或旧快照。现有 2 秒 bundle 响应缓存和 mutation 后缓存失效机制保持不变，因此同一只读窗口内可复用结果，成功切档后会重新读取。

## 鉴权与数据安全

继续服从 dashboard 的 operator read access；切档仍服从既有 write/admin 权限。不得读取或输出凭证，不触发任何交易或资金操作。

## 错误处理与幂等性

权威读取超时或失败时沿用 panel 级 `error`/`timeout` 返回，不再把缺字段的占位对象伪装成已确认状态。只读请求保持幂等。

## 状态迁移与生命周期

不改变策略档位的激活、暂停自动换档或恢复自动换档生命周期；只改变查询结果的来源。

## 缓存与性能

`aiConfigModel` 是控制面真实性数据，优先保证当前状态准确。请求内与 `aiRuntime` 共用一次权威 AI runtime 查询，避免重复远程调用；bundle 的短缓存继续限制重复读取开销。

## 日志、监控与审计

沿用现有 dashboard bundle panel timing、timeout 和 slow 日志。策略档位切换审计记录不变。

## 测试策略

- 回归测试：即使 snapshot 中存在不完整的 `aiConfigModel` 占位值，bundle 也必须返回权威 `strategy_profile`。
- 兼容测试：其他 P2 dashboard panels 仍从 snapshot plane 读取。
- 运行相关单元测试、完整 unit suite 与 Ruff；最窄集成测试在 WSL2 执行。

## 迁移、回滚与兼容性

无迁移。响应字段只会从“不完整”恢复为既有完整协议，前端向后兼容。回滚时移除 `aiConfigModel` 的权威请求时读取例外即可。

## 配置与环境隔离

不新增环境变量，不读取 `.env.*`。Windows、WSL2、模拟栈和门禁 live profile 使用同一代码路径。

## 代码组织与依赖

不新增依赖，不重构 snapshot plane；采用 auth route 中的最小路由决策变更。

## 文档与运维

本文件记录故障边界与验收标准。无需修改部署入口或运维流程。

## 部署与验收标准

- 新安装未手动切档时显示初始化激活的 `trend_normal（趋势标准）`。
- 手动切到任一档位后，刷新或 mutation 后首次读取显示真实当前档位，不显示“待确认”。
- 不改变自动换档开关、交易执行和风控状态。
- 相关测试与静态检查通过后才可按既有流程部署；本任务不执行部署。
