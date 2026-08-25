# FS-010 Managed Profile 未知配置键失败关闭设计与实施范围

> 文档状态：Phase 3P 已实施；目标启动验证与独立复核开放  
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3P 整改  
> 核对范围：四个 managed strategy YAML、managed profile loader、配置生成器/参考文档与相关单测  
> 运行时边界：未读取 `.env.*`，未连接数据库、交易所或账户，未启动服务、Docker 或 WSL2  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段修复 `FS-010`：删除四个 managed profile 中看似可调、实际被
`AATSSettings(extra="ignore")` 静默丢弃的 `strategy_profile_auto_rollback_enabled`，
并让 managed profile loader 对任何未知 settings key 失败关闭。

本阶段不实现新的自动回滚行为。现有 release-effectiveness、RDP observation 或
profile recommendation rollback 各有独立治理状态与安全边界，不能通过增加一个未接线
Settings 字段把它们伪装成统一 runtime 开关。

## 2. 整改前行为与根因

四个 `configs/strategy_profiles/*.yaml` 都声明
`strategy_profile_auto_rollback_enabled: true`，生成器和现行配置文档也把它列为可调字段；
但 `AATSSettings` 没有此字段且配置为 `extra="ignore"`。managed loader 合并 YAML 后直接
`model_validate`，因此该键无日志、无错误地消失。

根因是 managed YAML 没有在合成边界与 Settings schema 做全量键集合校验；全局
`extra="ignore"` 为 legacy/manual config 兼容而保留，却被错误地用于受管配置。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `managed_profiles.py` | 读取 strategy YAML 后验证 mapping 和全部 key；未知 key 抛确定性错误 |
| 四个 `strategy_profiles/*.yaml` | 删除没有 runtime 消费者的伪开关及误导注释 |
| `generate_managed_config_artifacts.py` | 不再生成/推荐该无效字段 |
| `configs/README.md` / managed reference | 明确只有 `strategy_profile_auto_control_enabled` 是当前换档主开关；回滚不由该伪字段控制 |
| 单元测试 | 证明当前四 profile 零未知键、拼写错误失败、合法键通过、旧伪键不再被宣传 |

managed profile key 的唯一 schema 是 `AATSSettings.model_fields`。环境模板中基础设施专用
键仍由既有 env artifact 测试单独治理，不进入 strategy YAML 校验。

## 4. 输入/输出接口

`load_managed_profile_values(profile, project_root=...)` 签名保持不变。行为收紧：

- 空 YAML 仍等价于空 mapping；
- 非 mapping YAML 抛 `managed_profile_strategy_tuning_must_be_mapping`；
- 任何非字符串或不在 `AATSSettings.model_fields` 的 key 抛
  `managed_profile_contains_unknown_settings_keys`，包含 profile、来源路径和排序后的 key；
- 合法配置继续返回 runtime defaults 与 strategy tuning 的合并 mapping。

不改变环境变量、API、数据库或前端接口。

## 5. 数据库 schema、表、索引与约束

无数据库 schema、migration、table、index 或 constraint 变更；不读取或写入数据库。

## 6. 事务、一致性与并发

配置加载发生在单进程启动阶段，无事务或共享并发状态。校验必须在返回合并值以及任何
runtime 构建前完成，使同一文件在 generator、测试和 runtime 调用中得到同一失败结果。

## 7. 授权、认证与数据安全

无授权/认证变化。不读取 `.env.*` 或任何凭证。错误只记录 profile、受版本控制的 YAML
路径和 key 名，不记录配置值。

## 8. 错误处理与幂等

- strategy 文件不存在：保持现有行为，仅使用 runtime defaults；
- 空文件：使用空 mapping；
- list/scalar：确定性失败，不让 `dict.update` 抛含糊异常；
- 未知/非字符串 key：按字符串表示排序后一次性报告全部键；
- 同一输入重复加载：返回相同 mapping 或相同错误；
- legacy/manual `load_yaml_config` 暂不切换全局 `extra="forbid"`，避免把本阶段扩大为旧配置迁移。

## 9. 状态转换与生命周期

```text
managed profile selected
  -> runtime defaults
  -> read strategy YAML
  -> require mapping
  -> require every key in AATSSettings.model_fields
  -> merge and continue startup

mapping/key validation failure -> startup/config generation stops before runtime build
```

删除伪键不改变当前有效 runtime 值，因为整改前它从未进入 Settings。

## 10. 缓存与性能

无缓存。每次 managed profile 加载增加一次 O(number of keys) 集合比较；配置文件规模远小于
业务数据，开销可忽略。

## 11. 日志、监控与审计

本阶段使用异常作为失败证据，不新增包含配置值的日志。错误应可由启动器/测试直接看到，
避免“配置文件存在但设置无效”。审计文档保留原始静默忽略事实并追加修复后状态。

## 12. 测试策略

新增 FS-010 对抗测试：

1. 四个当前 managed YAML 的 key 全属于 Settings schema；
2. 四个 YAML、生成器和现行配置文档不再声明伪键；
3. 临时 profile 中未知 key 触发确定性错误并包含 key/path；
4. 多个未知 key 一次性排序报告；
5. 非 mapping YAML 失败关闭；
6. 已知 managed key 正常合并；
7. 直接 `load_managed_profile_values` 与 `load_settings` 都不能绕过校验。

运行 focused、settings/env/generator related、全量 unit 与 Ruff。

## 13. 迁移、回滚与兼容

删除 `strategy_profile_auto_rollback_enabled` 不改变现行 runtime，因为旧值始终被忽略。
这是配置真实性修复，不提供继续接受该键的兼容开关。

任何外部分支若仍写该键，在合并后会明确失败，维护者必须删除它或先设计真实、可审计、
端到端受测的自动回滚控制面。回滚到静默 ignore 会重新引入 FS-010，不应作为生产方案。

## 14. 配置与环境隔离

严格校验只作用于四个 managed strategy YAML 及其 loader；不扫描 `.env.*`，不读取 secret，
不改变 legacy/manual YAML 路径。本阶段不使用 live profile 启动，只做版本控制文件和临时
测试目录验证。

## 15. 代码组织与依赖

预计修改：

- `aats/bootstrap/managed_profiles.py`；
- 四个 `configs/strategy_profiles/*.yaml`；
- `scripts/generate_managed_config_artifacts.py`；
- `configs/README.md` 与 `docs/configuration/managed-config-reference.md`；
- 新增 `tests/unit/test_fs010_managed_profile_unknown_key_fail_closed.py`；
- 全系统审计、code review、配置/测试现行入口。

不新增第三方依赖。

## 16. 文档、运维手册与验收标准

本阶段验收：

- 当前四个 managed strategy YAML 零未知 Settings key；
- 旧伪开关从 YAML、生成器和现行配置说明移除；
- 未来任意 typo/未知 key 在 managed loader 边界失败，不进入 runtime；
- 合法 managed profile 行为保持；
- focused、related、full unit、Ruff、文档链接和 diff check 通过，或准确披露环境阻塞；
- FS-010 更新为代码关闭/目标启动验证开放，不把不存在的开关写成已实现；
- 真实资金生产继续 NO-GO。

最终运行层关闭还需在后续 committed candidate 上由标准入口加载四个 managed profile，
证明错误键阻断发生在任何外部副作用前，并由独立复核确认没有仓库外配置仍依赖该伪键。

实施与验证结果见
[`36-fs-010-managed-profile-unknown-key-fail-closed.md`](../../audit/full_system_2026_08_24/36-fs-010-managed-profile-unknown-key-fail-closed.md)。
