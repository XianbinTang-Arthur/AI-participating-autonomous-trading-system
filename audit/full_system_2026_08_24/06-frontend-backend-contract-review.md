# 06 前后端契约审查

> 状态边界：本文件主体保存 Phase 1/2 修复前快照。Phase 3B/3M 已把 FS-001 rollback
> 与 apply 都改为授权后无写入 `501`；Phase 3D 已让 `/healthz` 对已结束关键 task 返回
> `503`。真 reverse saga、完整 deployment readiness 与 task hang/lag 仍 OPEN；
> Phase 3E 另已让 Gateway 在任何 readiness/background 前只读校验
> RDP schema，失败阻断 lifespan；真 Compose/clone 验证仍 OPEN。当前证据
> 分别以 `22`、`33`、`24`、`25`、`20` 为准。

## 接口面

当前 app 导入后有 193 条 HTTP 路由，GET 140、POST 51、PATCH 1、DELETE 1。核心 router 分为 operator、auth、UI、RDP、RDP profile。大多数 mutation 使用 `require_write_access`/`require_admin_access`，读接口使用 `require_read_access`；dashboard 通过 snapshot plane 聚合大量后端查询并在 mutation 后失效缓存。

## 主要契约问题

### FS-001：rollback 的 HTTP 成功语义不真实

Profile rollback 返回 `ok:true` 与 terminal `rolled_back`，但效果仅是治理行状态变化。`pending_live_rollback:true` 不能抵消顶层成功语义；客户端若只判断 2xx/ok 会产生危险误解。该 endpoint 应在效果未完成时使用非成功状态，或返回明确 operation state 并禁止 terminal `rolled_back`。

Phase 3B 已按上述要求把 rollback 改为零写入 501。Phase 3M 又证明旧 profile apply
Saga 的数据库四步不等于 runtime 生效，并将 apply 同样改为零写入 501。当前客户端
不得把 approve/release 解读为已生效；真实 activation/reverse saga 与 worker readback
完成前，profile apply/rollback 均不可用。

### FS-012：系统 liveness 与功能 readiness 混用

Phase 3D 后，`/healthz` 还会在 lifespan runtime 已观测到关键 task 结束时返回
`503`；其余情况下仍只表示 FastAPI lifespan 存活。部署脚本继续把它作为上线
成功条件，仍缺机器可判定的 `/readyz`/deployment gate，至少应包含 recovery、
reconciliation、account freshness、critical task last-success/lag、active
parameters 与 required daemon。

Phase 3E 已关闭 FS-012 中“RDP migration/validation 错误被吞后 Gateway 仍 ready”的代码路径：校验在 `build_runtime`、readiness 和后台 task 之前，且不做 DDL。这不会把 `/healthz` 升级为 trading readiness；上述其余 packet 缺口保持。

## 已确认的良好实践

- DB unavailable 与 constraint violation 有 503/422 显式映射，避免一律 500。
- 动态客户端动作对未知 action 倾向 fail-closed；权限不足与 transport failure 在 UI 文案中区分。
- mutation 后会失效 bundle cache，并按 path 对高优先 panel 做 eager refresh。
- UI 对 fee 成本/返佣、hold/no-trade、deferred loading、权限拒绝等有专门语义测试。
- session version 让禁用用户、改角色/密码后的旧 session 可失效。

## 需要补齐的契约测试

1. 对每个 mutation 建立“状态前置条件—HTTP—持久化副作用—runtime 读回—审计记录”矩阵。
2. profile rollback 必须断言 active parameter/runtime payload 已真实回退，而非只断言 200。
3. OpenAPI/response schema 与 43 个 JS 模块使用字段做自动 drift 检查；当前主要依赖集成字符串/Node smoke 测试。
4. snapshot panel 应暴露 `as_of`、source、freshness、partial/degraded；客户端不得把旧缓存当当前事实。
5. 所有异步 operation 使用 `accepted/pending/succeeded/failed`，禁止把“请求已登记”叫作“已完成”。

## 未知项

- 未在真实浏览器完整点击 193 条路由对应流程。
- 未验证低速网络、session 临界过期、多标签页 mutation、浏览器后退缓存和大 payload。
- 未核对每一个 JS 字段与 Pydantic schema；覆盖矩阵对此保持 PARTIAL。
