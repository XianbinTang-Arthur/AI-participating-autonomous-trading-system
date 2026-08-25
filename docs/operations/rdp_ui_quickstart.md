# RDP UI 速查 —— 5 分钟上手

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：现行 UI 速查。最后核对：2026-08-24（起始 HEAD `00b6df0` + 未提交 Phase 3F 覆盖层）。当前只使用 `derivatives` 模拟入口 `http://127.0.0.1:8001`；旧 `https://127.0.0.1:8011` live 路径已被标准部署入口禁用。


> 目标: 打开 `http://127.0.0.1:8001/ui/rdp` 后，不用查文档就知道该看什么、该点什么。
> 深入 SOP 请看 [rdp_operator_workflow.md](./rdp_operator_workflow.md) 和 [operator_checklist.md](./operator_checklist.md)。

---

## 1. RDP 在做什么?

后台 scheduler 只运行 8 个 enabled workflow；`decision_cycle` 和 `release_cycle` 当前禁用，后者还禁止入队。研究与建议生成不是所有时刻都“自动一直跑”。
**建议不会自动生效**, 必须 operator 在 UI 上点一下才会影响实盘。

每条建议绑定一个 `(family, timeframe)` 组合, 例如 `DIRECTIONAL / 1H`。
建议有五种类型:

| 类型 | 含义 | 影响实盘? |
|------|------|----------|
| **parameter_upgrade** | 换一组新参数 | ✅ **会改实盘**, 审批要慎重 |
| **keep_active** | 维持现状 | ❌ 不改 |
| **lower_priority** | 降低此策略优先级 | ❌ 不直接改参数 |
| **pause** | 暂停此策略交易 | ✅ **会停止此 combo 下单** |
| **require_review** | 数据异常需人工看 | ❌ 只是标记 |

---

## 2. 屏幕四个区块 —— 自上而下看

```
┌─────────────────────────────────────────────────┐
│ ① Hero 顶带:  待审批N 观察中M 阻断K 队列L       │ ← 一眼看整体
├─────────────────────────────┬───────────────────┤
│ ② 当前阻断(红色, 有才显示)   │                   │
│ ③ 当前待处理(待审批卡)       │  ⑤ 右侧运行态栏   │
│ ④ 待发布候选(已批准待发卡)   │  (服务健康/Gate) │
│ ⑥ 观察与回滚                 │                   │
└─────────────────────────────┴───────────────────┘
```

### 各区块看什么

- **② 当前阻断** — 红色告警。不处理这些, 后续审批都会被挡。先清它。
- **③ 当前待处理** — `draft` 状态的建议, 等你决策。**主战场**。
- **④ 待发布候选** — 已审批(approved)但还没 apply 到实盘的。下一步是 Gate + release。
- **⑥ 观察与回滚** — 已 apply 的 release 在观察窗内(默认 24h), daemon 自动评估效果, 期间你也可以手动跑观察 / 触发回滚。
- **⑤ 运行态栏** — RDP daemon 健康, 最近一次 Gate / apply 结果。出问题这里先变色。

---

## 3. 按钮速查 —— 点下去会发生什么

### 审批类 (在 ③ 待处理卡上)

| 按钮 | 走的 API | 点完之后 |
|------|---------|--------|
| **批准参数候选** | `POST /rdp/recommendations/{id}/approve` | rec 变 approved, 出现在 ④ 待发布候选; **不改实盘** |
| **同意保持当前** | 同上 | rec 变 approved, 这轮不创建新发布; **不改实盘** |
| **同意降优先级** / **同意暂停** | 同上 | rec 变 approved, 治理侧记录; **pause 会停止此 combo 下单** |
| **退回 / 拒绝** | `POST /rdp/recommendations/{id}/reject` | rec 变 rejected, 卡片消失 |
| **批准并发布** | `POST /rdp/recommendations/{id}/approve-and-release` | **一键跑完 approve + Gate + release + apply**, 实盘立刻变参数, 进观察窗 |

### 发布类 (在 ④ 待发布候选卡上)

| 按钮 | 走的 API | 点完之后 |
|------|---------|--------|
| **运行 Gate** | `POST /rdp/gates/run` | 跑预检(波动率/完整性阈值), 失败会说明阻断原因 |
| **创建发布** | `POST /rdp/releases/create` | 建 release 记录 + apply 到实盘; **会改实盘** |

### 观察 / 回滚 (在 ⑥ 观察与回滚卡上)

| 按钮 | 走的 API | 点完之后 |
|------|---------|--------|
| **运行观察** | `POST /rdp/observations/run` | 在观察窗内评估此 release 效果 |
| **执行回滚** | `POST /rdp/parameters/rollback` | **实盘参数回到上一版**; 需二次确认 |

### 调优 (⑥ 附近的 tuning 卡, 可能隐藏)

| 按钮 | 作用 |
|------|------|
| **批准调优提案** | research 侧的默认值 override 生效 |
| **拒绝调优提案** | 不采纳 |

---

## 4. 典型一天 —— 5 分钟节奏

1. 打开 `http://127.0.0.1:8001/ui/rdp`
2. **看 ① Hero 四个数字**:
   - 阻断 > 0 → 先去 ② 看阻断, 处理完再审批
   - 待审批 > 0 → 去 ③ 逐个审批
   - 观察中 > 0 → 去 ⑥ 看是否触发回滚建议
3. **审批流**: 在 ③ 每张卡上, 根据 recommendation_type 决定:
   - `parameter_upgrade` + 置信度高 + 数据无阻断 → 点 **批准并发布** (一键进入实盘)
   - `parameter_upgrade` 但想分步看 → 先 **批准参数候选** → 去 ④ 跑 **运行 Gate** → 通过再 **创建发布**
   - `keep_active` / `lower_priority` / `pause` → 点对应的"同意..."按钮, 确认即可
   - 置信度低 / 数据存疑 → 点 **退回 / 拒绝**
4. **观察期**: 已发布的进 ⑥ 等 24h, daemon 自动评估。触发 rollback 建议时手动决定是否 **执行回滚**。
5. 看 ⑤ 运行态栏, 全绿关掉 tab。

---

## 5. 常见提示怎么办

| 顶部横幅 | 含义 | 怎么办 |
|---------|------|--------|
| `Step2 数据完整性未通过` / `integrity_blocked=true` | research 侧快照缺数据 | 去 ⑤ 看是否有"刷新数据"workflow, 触发 `data_maintenance`; 或等 daemon 自己补 |
| `Pre-apply gate 阻断 (blocked_by_gate)` | 发布前预检不通过 | 看阻断原因(通常是波动率/数据完整性阈值), 可等更多数据再试 |
| `审批已落库但 apply 失败` | approve 成功但进实盘失败 | 去 ④ 看对应 release 的 apply_result, 手动 **创建发布** 重试 |
| `409 / cas_race / 被并发改写` | 另一个 operator 抢先操作了 | 刷新页面, 看最新状态 |
| `Internal Server Error` (红色 danger 横幅 60s TTL) | 后端 bug | 截图 + 记下时间, 报给开发者; **不要**一直重试 |
| `卡片消失, 没出现下一条` | 此 combo 下 DB 里**没有**下一条 pending | 正常。等 research 下一轮产出。不是 bug |

---

## 6. 几条硬纪律

1. **parameter_upgrade 审批一定要看置信度 + 数据完整性**, 二者任一不达标就退回。置信度低的参数升级进实盘的风险高于"维持现状"。
2. **"批准并发布"是原子操作**, 一旦点下 Gate 和 apply 都会串行跑完。适合高置信度 + 无阻断的场景。不确定就分步(批准参数候选 → 跑 Gate → 创建发布)。
3. **回滚只撤参数, 不撤 release 记录**。回滚后那条 release 在审计里仍然存在, 标记 `rolled_back`。
4. **操作后别立即第二次点**, 后端有 CAS 保护但前端 8s flash 覆盖时你可能误以为失败。等刷新完成再判断。
5. **dashboard 默认 2 分钟 polling**, 按按钮触发的操作会立刻 manual refresh 一次。观察窗口内状态变化可能要等下一轮 polling 才能看到。

---

## 7. 深入阅读 (按需)

- **每日/每周巡检清单**: [operator_checklist.md](./operator_checklist.md)
- **完整 SOP (含 API 调用示例)**: [rdp_operator_workflow.md](./rdp_operator_workflow.md)
- **参数 apply/rollback 细节**: [parameter_apply_and_rollback.md](./parameter_apply_and_rollback.md)
- **治理语义**: [parameter_governance.md](./parameter_governance.md)
- **周期性复盘流程**: [periodic_review_workflow.md](./periodic_review_workflow.md)
- **生产参数变更 runbook**: [production_parameter_change_runbook.md](./production_parameter_change_runbook.md)
- **RDP 可靠性 runbook**: [rdp_reliability_runbook.md](./rdp_reliability_runbook.md)
