# FS-015 回放与生产 Short-Bias 门控一致性设计与实施范围

> 文档状态：Phase 3R 已实施；历史证据重跑与独立复核开放
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3Q 整改
> 核对范围：生产 independent scoring、replay 参数/适配器、回测参数入口、实验参数序列化及相关测试
> 运行时边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动服务、Docker 或 WSL2
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段修复 `FS-015`：当 `strategy_short_bias_enabled=false` 时，independent replay
必须像生产评分一样把 short leg 原始评分钳制为 `0.0`，不得继续选择 short dominant leg。

本阶段不宣称 OHLCV replay 与生产输入、撮合或 AI assessment 完全等价，不重跑历史研究
artifact，也不改变 live managed profile 当前为 `true` 的配置。

## 2. 整改前行为与根因

生产 `compute_raw_book_score()` 在 short leg 且开关关闭时立即返回 `0.0`；replay
`IndependentReplayAdapter.evaluate_bar()` 则无条件计算 long/short 并取较大值。若配置切换为
long-only，研究仍可能模拟开空，而生产不会，导致研究结论与目标运行配置失配。

根因是 `ReplayParameterOverrides` 没有承载该策略上下文，适配器也没有对应 gate；生产端
注释长期接受了这一差异，却没有可执行的一致性契约。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `scoring.py` | 保持生产 short gate 真源，并移除已经解决的差异说明 |
| `ReplayParameterOverrides` | 以生产同名布尔字段承载、校验并序列化目标 short-bias 上下文 |
| `IndependentReplayAdapter` | 在 short score 进入历史、dominant 选择与状态机前执行 gate |
| active-parameter mapping | 明确该字段是 replay 上下文，不是按 family/timeframe 自动发布的研究调优参数 |

## 4. 输入/输出接口

回测与 replay 现有 `--param key=value` 接口支持：

```text
--param strategy_short_bias_enabled=false
```

CLI 通过 JSON 解析得到真正的布尔值。字符串、数字、列表等非布尔输入必须失败，不能用
Python truthiness 把 `"false"` 错当成开启。`to_dict()` 和实验登记必须保留该字段。

## 5. 数据库 schema、表、索引与约束

无 schema、migration、table、index 或 constraint 变更。实验 registry 继续使用现有 JSON
参数列，只新增一个可序列化键。

## 6. 事务、一致性与并发

无新增事务或并发状态。门控在单 bar 同步评估内完成，并且发生在 score history 更新、
dominant-leg 选择和状态迁移之前，避免残留 short score 污染后续 bar。

## 7. 授权、认证与数据安全

该开关不是凭据，也不触发交易。测试只构造内存 settings、bar 与 replay context；不读取
任何 `.env.*` 或账户数据。

## 8. 错误处理与幂等

- 布尔 `true/false`：确定性接受；
- 字段缺失或 JSON `null`：使用兼容默认 `true`；
- 非布尔值：构造/反序列化时 `ValueError` 失败关闭；
- 重复 replay：相同 bar 与参数产生相同 gate 结果。

## 9. 状态转换与生命周期

```text
bar + params
  -> long score
  -> short enabled ? compute short score : short score = 0.0
  -> append gated histories
  -> choose dominant leg
  -> stability / edge / state machine
```

若 long score 同样为 `0.0`，既有 tie-break 选择 long；关闭 short 时不存在任何 short-dominant
或新开 short 路径。

## 10. 缓存与性能

关闭 short 时跳过一次 `_compute_book_score()`，无新增缓存、I/O 或显著开销。开启时行为和
计算量保持不变。

## 11. 日志、监控与审计

不增加逐 bar 日志。实验参数 JSON 是复现证据，必须包含 gate 值；审计只证明代码和隔离
测试一致，不把它扩写成历史 artifact 已重跑或目标环境已验证。

## 12. 测试策略

新增对抗测试覆盖：

1. 参数默认值、布尔 round-trip 与实验字典可见性；
2. 非布尔 direct/from-dict 输入失败关闭；
3. bearish golden vector 在开启时可产生 short dominant，在关闭时 short score 恒为零；
4. 生产与 replay 对同一个关闭值均输出 short raw score `0.0`；
5. CLI JSON `false` 到 replay 参数的传播；
6. 源码不再保留“已知差异待处理”注释。

运行 focused、replay/scoring/active-parameter related、Ruff 与全量 unit。

## 13. 迁移、回滚与兼容

兼容默认 `true` 对齐当前 derivatives/derivatives_live tracked profile 以及既有 replay 行为。
面向某个 profile 生成正式研究证据时，调用方必须把该 profile 解析后的实际值显式写入
参数；历史未包含此字段的 artifact 只能按旧默认解释，不能声称验证了 long-only 配置。

回滚本修复会重新引入配置关闭后回放开空的已确认漂移，不应作为生产方案。

## 14. 配置与环境隔离

该字段与生产同名，但不加入 RDP active-parameter 自动映射。原因是生产字段为全局策略
能力开关，而 active sets 按 family/timeframe 分片；自动映射会允许多个 combo 对同一全局
开关产生顺序相关覆盖。它只作为 replay 目标配置快照。

## 15. 代码组织与依赖

预计修改：

- `aats/data_platform/replay/core/replay_context.py`；
- `aats/data_platform/replay/adapters/independent_adapter.py`；
- `aats/services/strategy_engines/independent/scoring.py`；
- active-parameter 注释/现行参数参考；
- 新增 `tests/unit/test_fs015_replay_short_bias_parity.py`；
- 当前文档与全系统审计状态。

不新增第三方依赖，不修改生产 public API。

## 16. 文档、运维手册与验收标准

本阶段验收：

- replay 在 gate 关闭时不计算、记录、选择或开启 short；
- 参数类型严格、可通过 CLI 输入且可在 artifact 中复现；
- 生产/回放 golden vector 锁定关闭语义；
- 当前参数参考与代码映射一致；
- focused、related、full unit、Ruff、文档链接和 diff check 通过，或准确披露环境阻塞；
- FS-015 更新为代码关闭/历史证据重跑与独立复核开放；
- 真实资金生产继续 NO-GO。

最终关闭仍需在 committed candidate 上独立复核，并以显式 gate 值重跑受影响研究证据；
本地单元测试不证明历史收益、真实撮合或 live 状态。

实施与验证结果见
[`38-fs-015-replay-short-bias-parity.md`](../../audit/full_system_2026_08_24/38-fs-015-replay-short-bias-parity.md)。
