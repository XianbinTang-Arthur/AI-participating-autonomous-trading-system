# 36 FS-010 Managed Profile 未知配置键失败关闭记录

> 文档状态：现行整改证据  
> 阶段：Phase 3P  
> 核对日期：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作分支：`codex/fs-002-kill-switch-p0`，变更尚未提交  
> 验证边界：版本控制内的 managed profile、Settings schema、生成器、参考文档与 Windows 单元测试；未加载 `.env.*`，未启动服务或目标 profile  
> 安全边界：未连接数据库、Redis、NATS、交易所或账户，未启动容器，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 结论

Phase 3P 修复了 `FS-010` 的静默配置失真：四个 managed strategy YAML 中原有的
`strategy_profile_auto_rollback_enabled` 没有 `AATSSettings` 字段，也没有行为消费者，
整改前会被 `extra="ignore"` 无提示丢弃。该伪键和误导注释现已从四个 YAML、配置
生成器与现行字段参考删除；没有用一个未接线 Settings 字段伪装“自动回滚已实现”。

`load_managed_profile_values()` 现在在合并前验证 managed runtime defaults 与 strategy
YAML 的全部 key：YAML 必须是 mapping，且每个 key 必须属于
`AATSSettings.model_fields`。未知或非字符串 key 会在 runtime 构建前抛确定性异常，
不能继续进入 `load_settings()`。

当前裁定：

**FS-010：CODE REMEDIATED / MANAGED UNKNOWN-KEY FAIL-CLOSED / TARGET STARTUP VERIFICATION OPEN**。

这不是生产运行 CLOSED。尚未在 committed candidate、标准启动入口或目标容器中验证
四个 profile，也没有证明仓库外 overlay 不再依赖旧伪键。

## 2. 原始缺陷与根因

整改前四个 `configs/strategy_profiles/*.yaml` 都声明：

```yaml
strategy_profile_auto_rollback_enabled: true
```

生成器和 `managed-config-reference.md` 又把它列为可调字段，但
`AATSSettings.model_fields` 中没有此键。全局 `extra="ignore"` 是 legacy/manual 配置的
兼容策略；managed loader 却直接把 YAML 合并后交给 Settings，导致该键既不报错、也
不生效。受控集合比较确认它是四个当前 managed YAML 中唯一未知的 Settings key。

可信影响是：Operator 或维护者可能相信自动回滚可由这个布尔值控制，实际 runtime
完全不读取它；未来拼写错误也会以同样方式静默进入版本控制。

## 3. 实施内容

### 3.1 删除不存在的控制能力

- 从 `spot`、`spot_live`、`derivatives`、`derivatives_live` 四个 strategy YAML 删除伪键
  和“常用可调”注释；
- 从 `STRATEGY_FIELD_GROUPS` 与 managed reference renderer 删除该字段；
- `configs/README.md` 明确：当前只有 `strategy_profile_auto_control_enabled` 是自动换档
  主开关；自动回滚没有统一 runtime Settings 开关；
- 保留现有 release-effectiveness、RDP observation 与 profile recommendation rollback
  的各自治理边界，不把不同工作流伪装成一个统一自动回滚状态机。

删除不改变整改前的有效 runtime 值，因为该键从未被 Settings 接受或被行为代码消费。

### 3.2 Managed 边界严格校验

`_validate_managed_profile_settings_keys()` 以 `AATSSettings.model_fields` 为唯一允许集合，
同时检查代码内 runtime defaults 与 YAML mapping。未知 key 按字符串排序并一次性报告：

```text
managed_profile_contains_unknown_settings_keys:
profile=<profile>:source=<source>:keys=<sorted keys>
```

非 mapping YAML 返回独立错误：

```text
managed_profile_strategy_tuning_must_be_mapping:
profile=<profile>:source=<path>
```

异常只包含 profile、版本控制内来源路径和 key 名，不包含配置值或 secret。空文件仍按
空 mapping 处理；策略文件不存在时保持既有 runtime-default 行为。

### 3.3 阻止文档生成回退

复核发现 `generate_managed_config_artifacts.py` 的 reference renderer 落后于现行文档，
并会把人工治理的 `configs/README.md` 覆盖为旧的简版。Phase 3P 已同步 renderer 的
配置顺序、managed 派生字段、live 约束、active-parameter 真源和当前基线；测试要求
renderer 输出与受版本控制 reference 逐字一致。

生成器继续负责 `.env.*.example` 与 `managed-config-reference.md`，不再覆盖人工维护的
`configs/README.md`。这避免下一次生成操作恢复已经失效的配置说明。

## 4. 防御性验证

新增 `tests/unit/test_fs010_managed_profile_unknown_key_fail_closed.py`，覆盖：

1. 四个当前 YAML 与四组 runtime defaults 的 key 全属于 Settings schema；
2. 旧伪键不在 YAML、生成器或现行 managed reference 中；
3. reference 文件与 renderer 输出逐字一致，生成器不再写 `configs/README.md`；
4. 单个未知 key 失败并报告 profile、来源和 key；
5. 多个未知 key 一次性按序报告；
6. list 形式的 YAML 作为非 mapping 失败；
7. 合法 key 正常合并，空 YAML 保持兼容；
8. `load_settings()` 不能绕过 managed loader 校验。

当前静态集合证据：

```text
spot: yaml_keys=91 unknown=[] merged_keys=109
spot_live: yaml_keys=91 unknown=[] merged_keys=109
derivatives: yaml_keys=168 unknown=[] merged_keys=189
derivatives_live: yaml_keys=201 unknown=[] merged_keys=222
managed_reference_matches_generator=True
```

## 5. 测试记录

### 5.1 定向与相关回归

首次未指定 basetemp 的 focused 运行有 `2 passed, 6 errors`；六个 error 全部发生在
pytest 创建系统 `tmp_path` 前，原因为 Windows 系统临时目录 `PermissionError`，不是
测试断言失败。仓库内全新隔离 basetemp 结果：

```text
8 passed, 1 warning in 0.90s
56 passed, 1 warning, 2 subtests passed in 1.68s
```

相关组合包含 FS-010、env profile 与 Settings 测试。warning 是既存 `.pytest_cache`
创建告警。

### 5.2 仓库规定的原样命令

```text
.venv\Scripts\python.exe -m ruff check aats/ --fix
All checks passed!

.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
87 passed, 2 warnings, 1 error in 3.20s
```

唯一 error 发生在 `tmp_path` fixture setup：系统临时目录返回
`PermissionError [WinError 5]`；此前没有 assertion failure。

### 5.3 仓库内全新 basetemp 全量复跑

```text
4345 passed, 30 skipped, 1666 warnings, 85 subtests passed in 95.17s
```

30 项 skip 未计作覆盖。warning 仍主要是既存 SQLite datetime adapter deprecation、
LongShort poller AsyncMock 未 await 与 `.pytest_cache` 创建告警，由 FS-021 继续承接。

### 5.4 Lint 与文档生成一致性

```text
.venv\Scripts\python.exe -m ruff check \
  aats/bootstrap/managed_profiles.py \
  scripts/generate_managed_config_artifacts.py \
  tests/unit/test_fs010_managed_profile_unknown_key_fail_closed.py --fix
All checks passed!

managed_reference_matches_generator=True
```

### 5.5 文档与差异检查

```text
变更/新增 Markdown：87 files，382 local links，broken=0
git diff --check：exit 0（仅输出既存 CRLF 转换提示）
```

## 6. 未执行验证

没有执行 WSL2 integration、Docker、数据库、Redis、NATS 或交易所操作。当前变更尚未
提交；项目标准 Windows→WSL2 流程只拉取 committed code，不能用手工 rsync 或直接
Compose 旁路。没有读取或显示任何 `.env.*`。

以下仍为 UNKNOWN：

- committed candidate 通过标准 API/daemon 入口加载四个 managed profile；
- 配置错误是否在目标进程任何外部副作用前以清晰诊断退出；
- 仓库外部署 overlay、自动化脚本或人工配置是否仍写旧伪键；
- future generator 实际运行后全部受版本控制 artifact 是否保持 clean；
- 独立 reviewer 对“删除而非实现统一自动回滚”的架构裁定。

## 7. 剩余关闭条件

最终关闭 FS-010 至少需要：

1. 提交并冻结候选 commit，由独立 reviewer 复核字段所有权与无消费者事实；
2. 在不接触 live 资金的目标启动环境逐个加载四个 managed profile；
3. 注入临时未知 key，证明进程在网络、数据库迁移、任务启动或交易所访问前非零退出；
4. 扫描仓库外受控部署配置，确认没有依赖旧伪键；
5. 运行生成器并证明只更新其声明拥有的 artifact，工作区无意外文档回退；
6. 若未来需要自动回滚，另行设计状态机、授权、代次、审计、部分失败恢复和端到端测试，
   不能只重新增加一个 Settings 字段。

## 8. 当前裁定

已收敛：managed profile 中被静默忽略的伪配置、未来未知/拼写错误 key 无提示通过、
非 mapping YAML 的含糊合并错误，以及配置生成器覆盖现行文档的路径。

未收敛：目标进程启动验证、仓库外 overlay 盘点、生成器实际 clean-run、独立复核和
任何真实自动回滚能力。

**FS-010：CODE REMEDIATED / MANAGED UNKNOWN-KEY FAIL-CLOSED / TARGET STARTUP VERIFICATION OPEN。**  
**REAL-MONEY PRODUCTION：NO-GO。**
