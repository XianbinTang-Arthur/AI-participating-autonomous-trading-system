# 38 FS-015 Replay/Production Short-Bias 门控一致性记录

> 文档状态：现行整改证据
> 阶段：Phase 3R
> 核对日期：2026-08-25
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`
> 工作分支：`codex/fs-002-kill-switch-p0`，变更尚未提交
> 验证边界：生产/replay 评分门控、参数类型/序列化、CLI 覆盖、active-parameter 边界与 Windows 单元测试
> 安全边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未重跑历史研究，未启动容器，未部署
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 结论

Phase 3R 消除了 `FS-015` 已确认的配置语义漂移。整改前，生产
`compute_raw_book_score()` 在 `strategy_short_bias_enabled=false` 时把 short leg 返回
`0.0`；independent replay 却无条件计算 short score，并可能选择 short dominant leg、
推进开空状态。这一缺陷在当前 tracked derivatives profiles 的 `true` 默认下 dormant，
但配置切换为 long-only 后会使研究行为与目标生产行为不一致。

当前 replay 以生产同名 strict boolean 承载该上下文。关闭时，adapter 在 short score
history、dominant-leg、edge 和状态机之前跳过 short 计算并记录 `0.0`；开启时保持既有
行为。字段通过 CLI JSON、`from_dict()`、`to_dict()` 和实验参数 JSON 传播。

当前裁定：

**FS-015：CODE REMEDIATED / HISTORICAL EVIDENCE RE-RUN & INDEPENDENT REVIEW OPEN**。

## 2. 实施内容

### 2.1 Replay 参数契约

`ReplayParameterOverrides.strategy_short_bias_enabled`：

- 默认 `true`，对齐既有 derivatives replay 行为和 tracked derivatives profile；
- 面向具体 profile 的正式实验必须显式传入解析后的实际值；
- direct construction 与 `from_dict()` 都只接受真正的 `bool`；
- 数字、字符串、list、dict 等 truthy/falsy 替代值全部 `ValueError` 失败关闭；
- `to_dict()` 始终保留字段，历史无该字段 artifact 只能按兼容默认解释。

### 2.2 Adapter 门控顺序

当前顺序固定为：

```text
long score
  -> short enabled ? compute short : short = 0.0
  -> append gated score history
  -> choose dominant leg
  -> stability / edge / state machine
```

门控不放在 dominant 之后，也不只阻止最终 open；这避免禁用期间的 short score 污染
history 并在未来重新开启时影响稳定性判断。

### 2.3 Active-parameter 边界

该字段不映射回生产 active parameter。原因是 active sets 按 family/timeframe 分片，而
生产 `strategy_short_bias_enabled` 是全局能力开关；若多个 combo 自动写同一字段，结果会
依赖遍历/覆盖顺序。它被列入 `_RDP_REPLAY_ONLY_PARAMS`，只表示目标 profile 上下文快照。

生产端 gate 仍以 managed/resolved `AATSSettings` 为真源；replay 调用方负责把同一个目标
值显式写入实验证据。此次没有扩大 RDP 的生产配置发布权限。

## 3. 文档纠错

现行 `docs/operations/parameter_mapping_reference.md` 在本阶段被完整重核并重写。旧文档
仍声称 independent 只有 18 个映射、`signal_edge_scale_bps`/`score_stability_threshold`
未映射、`directional_trend_weight` 占位映射到 `strategy_entry_alpha_min`，并保留旧 replay
默认阈值；这些内容均与当前代码不符。

当前参考记录：

- independent 21 个 required 映射；
- directional 3 个实际映射、只有 `min_hold_seconds` required；
- directional trend weight 没有生产映射；
- short-bias 是 replay-only 目标上下文；
- independent/directional 默认阈值为当前 dataclass/`for_family()` 实际值；
- 静态映射不能证明数据库 active set 或 worker effective settings。

同时更正现行测试/代码审查文档中“future derivatives-live required list 仍漏两个
collector”的过期表述：代码清单已经包含两者，但 live 当前禁用，目标 health/freshness
仍未验证。

## 4. 防御性验证

新增 `tests/unit/test_fs015_replay_short_bias_parity.py` 17 项测试，覆盖：

1. 默认值、`false` 与缺失兼容语义；
2. 参数字典 round-trip 与 artifact 可见性；
3. direct/from-dict 的五类非布尔输入失败关闭；
4. CLI `--param strategy_short_bias_enabled=false` 产生真正布尔值；
5. 关闭时 short scorer 不被调用，history 只记录 `0.0`，dominant 不会是 short；
6. 真实 bearish bar vector：开启可开 short，关闭不能进入 short；
7. 生产与 replay 对同一 disabled value 的 short raw score 均为 `0.0`；
8. replay-only 白名单与两个 production mapping 都锁定该治理边界。

## 5. 测试记录

```text
focused isolated basetemp: 17 passed, 1 warning in 0.43s
related replay/scoring/CLI/mapping isolated basetemp: 132 passed, 1 warning in 1.40s
application Ruff: All checks passed!
new test Ruff: All checks passed!
```

仓库规定的原样全量命令：

```text
87 passed, 2 warnings, 1 error in 3.45s
```

唯一 error 是 pytest 创建 Windows 系统临时目录时的
`PermissionError [WinError 5]`，此前没有断言失败。仓库内全新 basetemp 的最终完整结果：

```text
4368 passed, 30 skipped, 1666 warnings, 85 subtests passed in 108.26s
```

warning 仍主要来自既存 SQLite datetime adapter、LongShort poller AsyncMock 与
`.pytest_cache`，由 FS-021 承接。

文档与差异检查：`93` 个变更/新增 Markdown、`400` 个本地链接、`broken=0`；managed
config reference 与 generator renderer 逐字一致；`git diff --check` exit `0`，仅输出
既存 CRLF 转换提示。本阶段四个直接新增/重写文档另行检查为零行尾空白。

## 6. 未执行验证与关闭条件

未执行 WSL2 integration、服务启动、Docker、数据库、Redis、NATS、交易所操作或历史
research artifact 重跑；没有读取 `.env.*`。当前变更未提交，也没有在 committed
candidate 上由独立 reviewer 复核。

最终关闭 FS-015 还需：

1. 用每个目标 profile 解析后的显式 gate 值盘点并重跑受影响历史 artifact；
2. 在 artifact/report 中确认字段、代码 commit、dataset version 与目标 profile provenance；
3. 扩展 AI mode、成本/edge、position state 等 production/replay golden vector；
4. 独立 reviewer 在 committed candidate 上复核 `true/false`、CLI 和 bearish vector；
5. 明确撤销或重新签署所有依赖旧 long-only replay 行为的研究结论。

## 7. 当前裁定

已收敛：关闭 short 后 replay 仍计算/记录/选择 short 的路径、非布尔 truthiness 歧义，以及
把全局能力开关误作为按 combo 自动发布参数的风险。

未收敛：历史证据重跑、其他生产/回放差异、committed candidate 独立复核和真实运行时
有效值验证。

**FS-015：CODE REMEDIATED / HISTORICAL EVIDENCE RE-RUN & INDEPENDENT REVIEW OPEN。**
**REAL-MONEY PRODUCTION：NO-GO。**
