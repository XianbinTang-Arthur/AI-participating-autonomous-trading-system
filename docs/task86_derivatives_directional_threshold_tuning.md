# 任务 86：合约 directional 参与度阈值微调

## 业务目标与边界
- 目标：按已确认方案，放宽合约 directional 在 BTC 高波动环境下的参与门槛。
- 边界：只调整合约 directional 的配置与自动档位 seed 基线，不改执行链、风控主逻辑和 public API。

## 模块职责与领域模型
- `configs/strategy_profiles/derivatives_live.yaml` 负责托管实盘合约调参基线。
- `aats/services/operator/strategy_profile_seed.py` 负责把当前运行时调参映射成 `trend_normal` 等注册档位。

## 输入输出接口
- 输入：托管 profile 加载后的 directional 阈值。
- 输出：运行时 settings、注册 profile snapshot、控制面策略档位摘要。

## 数据库 / 表 / 索引 / 约束
- 本任务不新增或修改数据库对象。

## 事务、一致性与并发
- 本任务仅变更配置与 seed 逻辑，不引入新的事务或并发语义。

## 鉴权、认证与数据安全
- 不新增鉴权入口。
- 不处理密钥或账户敏感信息。

## 错误处理与幂等
- 仍沿用现有配置加载与 seed 幂等更新路径。

## 状态流转与生命周期
- `derivatives_live` 加载新阈值。
- seed 生成 `trend_normal` 等档位时，合约基线保留这些新阈值，不再被旧 clamp 拉回。

## 缓存与性能
- 仅配置读取和轻量对象生成，无新增性能热点。

## 日志、监控与审计
- 不新增日志字段。
- 控制面可通过策略档位快照观察新阈值是否生效。

## 测试策略
- 单测：验证 `derivatives_live` 托管 profile 加载后的阈值。
- 集成：验证策略档位快照中的 `trend_normal` 基线与新合约阈值一致。

## 迁移、回滚与兼容性
- 兼容现有配置结构。
- 回滚方式：恢复 `derivatives_live.yaml` 与 seed 合约 clamp 范围。

## 配置与环境隔离
- 只影响 `derivatives_live` 托管 profile 与合约档位 seed。
- 不影响现货 runtime。

## 代码组织与依赖
- 维持现有目录结构。
- 不新增外部依赖。

## 文档与运维手册
- 本文档记录变更范围、验证方式与回滚点。

## 部署与验收标准
- 托管合约 live 配置加载出目标阈值。
- 合约策略档位快照中的 `trend_normal` 反映这些阈值。
- 相关单测与最窄集成测试通过。
