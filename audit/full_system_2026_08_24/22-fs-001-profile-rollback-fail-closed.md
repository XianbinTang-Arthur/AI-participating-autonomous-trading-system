# 22 FS-001 Profile Rollback 失败关闭收口记录

> 后续状态：本文件冻结 Phase 3B 的 rollback 收口证据。Phase 3M 又将同源的
> profile apply 错误成功路径改为无写入 `501`；FS-001 当前状态与验证证据见
> [`33-fs-001-profile-apply-fail-closed.md`](33-fs-001-profile-apply-fail-closed.md)。

> 日期：2026-08-24  
> 起始代码基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 当前工作区：`codex/fs-002-kill-switch-p0`，同时包含尚未提交的 Phase 3A FS-002 变更  
> 验证边界：静态代码、fake governance session/Request/Principal、Windows 全量单测  
> 未执行：真实 research/live DB、worker 热加载/读回、部署、凭证读取、真实账户/交易所操作  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 原始故障与修复前复现

`POST /rdp/profile-recommendations/{rec_id}/rollback` 在 token、actor 和双人签署校验后，只执行：

```sql
UPDATE governance.recommendations
SET status = 'rolled_back'
WHERE recommendation_id = :rid
```

请求中的 `to_parameter_set_id` 没有参与任何业务逻辑。隔离替身复现输出：

```text
result = {
  ok: true,
  recommendation_id: rec_fs001_before,
  status: rolled_back,
  pending_live_rollback: true
}
rolled_back_update_executed = true
commits = 1
```

复现结论：**PASS / 错误成功语义已复现**。此时 active set、apply history、live payload 和 worker runtime 没有变化。

## 2. 根因与新发现的架构边界

1. 路由把 recommendation 工作流标签误当成有效参数的事实状态。
2. 当前没有 profile reverse saga；也没有把 research active set、live 修订/payload、history 和 execution/runtime readback 绑定到同一 operation/generation 的契约。
3. 已有 `profile_apply_saga` 不能直接反向复用：它查询 live payload 中的 `profile_id`，而当前 activation model 使用 `active_profile_id`。
4. saga 直接写 live activation payload 的三个 threshold key，但它们不是 `StrategyProfileActivationState` 字段；当前 repository 用 Pydantic 重建状态时会忽略额外字段。
5. 未发现 execution worker 对上述三个字段的权威热加载/读回。因此“DB payload 已写”不能推导“runtime 已生效”。

这个架构边界意味着：本阶段若强行实现反向写入，会把已证明的虚假 rollback 换成更难发现的 research/live/runtime 漂移。

## 3. 修复决策

依照风险路线图“未完成真实 rollback 前先使 endpoint fail-closed/非成功”的要求，本阶段采用最小正确收口：

- 保留 `require_write_access`、v2 rollback token、session/token actor 一致与双人签署校验；
- 安全校验通过后稳定返回 `501 profile_rollback_not_implemented`；
- 不更新 recommendation，不 commit，不修改 active set/history/live payload；
- 响应不含 `ok=true`、`rolled_back` 或 `pending_live_rollback` 等可被误读为已完成的语义；
- 结构化 warning 只记录 recommendation/current status/actor/是否指定 target，不记录 token 或 parameter values。

完整设计与后续 reverse saga 验收条件见 `docs/task/fs_001_profile_rollback_fail_closed_sow_2026_08_24.md`。

## 4. 变更文件

| 文件 | 变更 |
|---|---|
| `aats/api/rdp_profile_routes.py` | 删除虚假 `rolled_back` UPDATE/commit/success body；改为授权后无写入 `501` |
| `tests/unit/test_fs001_profile_rollback_fail_closed.py` | 覆盖无写入失败、双签优先、重复请求幂等 |
| `docs/task/fs_001_profile_rollback_fail_closed_sow_2026_08_24.md` | Phase 3B 设计、实施边界与后续关闭条件 |
| `docs/code_review/README.md` | 纠正当前 endpoint 行为与不可用边界 |
| `audit/full_system_2026_08_24/*` | FS-001 证据、风险状态和 NO-GO 门禁 |

无 DB migration，无新依赖，无配置/凭证变更，无部署。

## 5. 验证证据

### 5.1 直接与相关回归

```text
.venv\Scripts\python.exe -m pytest \
  tests/unit/test_fs001_profile_rollback_fail_closed.py \
  tests/unit/test_profile_apply_saga.py \
  tests/unit/test_rdp_apply_token_v2.py \
  tests/unit/test_rdp_apply_token.py \
  tests/unit/test_rdp_routes_precheck.py \
  tests/unit/test_rdp_rollback_validation.py -q ...
50 passed in 1.42s
```

### 5.2 最终全量单测

```text
.venv\Scripts\python.exe -m pytest tests/unit/ -q -p no:cacheprovider \
  --basetemp=audit/full_system_2026_08_24/test-tmp/phase3b-fs001-full-final
4151 passed, 30 skipped, 1665 warnings, 85 subtests passed in 103.72s
```

警告与 Phase 3A 一致：主要是 Python 3.12+ SQLite datetime adapter deprecation，以及 `test_long_short_poller.py` 现有 AsyncMock `raise_for_status` 未 await。它们不在 FS-001 原子收口范围内。

### 5.3 Lint

```text
.venv\Scripts\python.exe -m ruff check aats/ tests/unit/test_fs001_profile_rollback_fail_closed.py
All checks passed!
```

### 5.4 真实 Postgres 集成测试

```text
.venv\Scripts\python.exe -m pytest \
  tests/integration/test_rdp_rollback_with_real_db.py -x -q ...
12 skipped in 0.03s
```

该套件要求 `AATS_RUN_POSTGRES_INTEGRATION=1`、testcontainers 和 psycopg2；当前未启用该隔离环境。它验证的是 combo active-parameter rollback，不是当前尚不存在的 profile reverse saga，所以即使未跳过也不足以关闭 FS-001。

## 6. 修复后对抗复测

使用与修复前相同的 fake governance session/Request/Principal：

```text
http_status = 501
detail.code = profile_rollback_not_implemented
rolled_back_update_executed = false
commits = 0
```

复测结论：**FAIL / 原虚假成功利用已被阻止**。修复后没有任何代码路径把该请求表述为 rollback 已完成。

## 7. 剩余风险与真 reverse saga 要求

尚未实现：

1. execution-owned profile/runtime 参数权威与热加载契约；
2. 持久化 rollback operation 、from/to digest、generation 与可重放步骤；
3. active set、live profile revision/payload、apply/rollback history 与 worker runtime 同 generation 读回；
4. 并发 apply/rollback 的 CAS/lock，以及部分失败的 pending/failed/补偿流程；
5. 空库、隔离 research/live 两库、worker ack 与故障注入集成测试；
6. 真正 rollback 后对目标参数的 runtime readback，而不是只查数据库标签。

在上述条件完成前，运维人员不得使用 profile recommendation rollback endpoint 处理实盘参数事故。

## 8. FS-001 最终状态

**PARTIALLY REMEDIATED / OPEN — 虚假成功和虚假终态已消除，真实 profile rollback 尚未实现。**

本修复收窄了失败影响：operator/自动化不再被告知“已回滚”。但它不提供参数事故的真实恢复能力，因此 G2 仍未放行。

## 9. 生产决定

**REAL-MONEY PRODUCTION: NO-GO**

FS-001 仍未关闭；FS-002 仍待真实四进程故障注入与独立复核；`FS-003/006/007/009` 等其他独立硬阻断仍然存在。本记录不是部署或上线授权。
