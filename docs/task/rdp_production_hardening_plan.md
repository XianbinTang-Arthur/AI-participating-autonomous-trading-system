# RDP 生产化整改计划

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 状态：待执行  
> 目标：将 RDP 从“研究与治理骨架”提升为“可安全作用于生产参数的受控子系统”  
> 适用范围：`aats/data_platform/`、`aats/api/rdp_routes.py`、`aats/services/operator/rdp_queries.py`、`scripts/rdp_task_daemon.py`、`deploy/wsl2-dev/`

---

## 1. 当前状态摘要

当前 RDP 已具备以下基础：

- 独立研究库与 governance schema
- recommendation / active decision / active parameter set 的基础数据模型
- workflow dispatcher 与 task queue 骨架
- operator UI 与 `/rdp/*` 读写入口

当前距离生产可用仍有明显缺口：

- 生产 apply 保护没有强制落地
- gate 主要依赖 artifact，而非主系统真实状态
- `/rdp/health` 不是可信的生产健康信号
- `rdp-daemon` 的 healthcheck 不能证明任务仍在推进
- 缺少真实的 RDP 端到端集成测试

因此，本计划按“先止血、再补保护、再补验证、最后放行”的顺序执行。

---

## 2. 执行原则

1. 在整改完成前，RDP 默认视为 `research-only`，不得直接影响生产参数。
2. 任何生产参数变更必须统一走 `approved recommendation -> gate -> release -> apply -> observation -> rollback` 标准链路。
3. 所有整改先在 `dev` 完成，再在 `staging` 完整演练，最后才允许 `prod` 试运行。
4. 未完成前 5 项硬 blocker 前，不得宣称 RDP 已具备生产能力。

---

## 3. 阶段划分

### 阶段 A：止血与强制保护

完成任务 1-4。

### 阶段 B：真实健康与执行器可靠性

完成任务 5-6。

### 阶段 C：端到端验证与准生产演练

完成任务 7-8。

### 阶段 D：生产观察与文档收口

完成任务 9-10。

---

## 4. 任务清单

## 任务 1：冻结生产 apply 能力

**优先级**：P0  
**目标**：先止血，禁止 RDP 在生产中直接写入 active parameter set。

**修改范围**

- `aats/api/rdp_routes.py`
- 如有需要，补一个全局开关配置项

**实施步骤**

- 为 `/rdp/parameters/apply`
- `/rdp/parameters/rollback`
- `/rdp/releases/create`

增加显式生产写保护开关，例如 `RDP_PRODUCTION_APPLY_ENABLED=false` 时直接拒绝。

**交付物**

- 生产默认拒绝 RDP 写操作
- 错误信息明确说明当前处于整改冻结期

**验收标准**

- `prod` 环境下直接调用上述写接口返回失败
- `dev` / `staging` 不受影响

---

## 任务 2：把环境策略接入所有写路径

**优先级**：P0  
**目标**：让 `dev/staging/prod` 从文档约定变成代码硬约束。

**修改范围**

- `aats/data_platform/operations/environment_guard.py`
- `aats/api/rdp_routes.py`
- `aats/data_platform/decision_system/active_parameter_apply.py`
- `aats/data_platform/production_workflow/release_registry.py`

**实施步骤**

- 所有生产写路径统一调用 `environment_guard`
- `prod` 强制：
- gate pass
- approval
- observation window = 72h
- 禁止 `skip_gate`
- `staging` 至少强制 gate

**交付物**

- 环境策略实际生效
- `prod` 下不再允许通过 API 参数绕过保护

**验收标准**

- `prod` 下 `skip_gate=true` 被拒绝
- `prod` 下自定义缩短观察窗口被拒绝
- `staging` 下未跑 gate 不能 apply

---

## 任务 3：将 pre-apply gate 升级为生产保护 gate

**优先级**：P0  
**目标**：gate 不再只是检查研究产物，而是评估主系统是否允许变更。

**修改范围**

- `aats/data_platform/production_workflow/pre_apply_gate.py`
- `aats/data_platform/production_workflow/gate_rules.py`

**实施步骤**

- 在现有 artifact 检查之外新增以下规则：
- 主系统 `/system/health`
- reconciliation 最近状态
- kill switch / trading halt 状态
- live DB 只读链路
- 最近错误率和异常事件
- decision / order intent 是否异常

**交付物**

- gate 结果能真实反映生产风险
- block / warn 原因可审计

**验收标准**

- 主系统 unhealthy 时 gate block
- reconciliation 异常时 gate block
- kill switch 开启时 gate block

---

## 任务 4：统一生产参数发布入口

**优先级**：P0  
**目标**：消除绕路 apply，统一 release 语义。

**修改范围**

- `aats/api/rdp_routes.py`
- `aats/data_platform/production_workflow/release_registry.py`

**实施步骤**

- 将 `/rdp/releases/create` 明确为唯一生产标准入口
- `/rdp/parameters/apply` 在 `prod` 中禁止直接使用
- release 必须自动带出：
- recommendation_id
- gate_result_ref
- previous_parameter_set_id
- release_id
- apply_result

**交付物**

- 生产 apply 不再有多条入口语义
- 所有生产变更都有完整 release record

**验收标准**

- `prod` 下直接 apply 被拒绝
- 生产 release 记录字段完整

---

## 任务 5：重写 RDP health，建立真实健康模型

**优先级**：P0  
**目标**：让 operator 上看到的 `healthy` 真正可信。

**修改范围**

- `aats/services/operator/rdp_queries.py`
- 如有需要，补充 supporting query helpers

**实施步骤**

- 将健康判定扩展为至少包含：
- governance DB 可连接
- governance 表可读写
- recent workflow run freshness
- task queue backlog
- daemon heartbeat freshness
- live DB 只读链路
- recent reliability check status

**交付物**

- `healthy / degraded / blocked / not_initialized` 等真实状态
- 细粒度 checks 明确指出降级原因

**验收标准**

- DB 断连时 health 降级
- daemon 心跳陈旧时 health 降级
- workflow 长时间未运行时 health 降级

---

## 任务 6：修复 rdp-daemon 心跳与健康检查

**优先级**：P0  
**目标**：证明 daemon “还在推进任务”，而不是“文件还在”。

**修改范围**

- `scripts/rdp_task_daemon.py`
- `deploy/wsl2-dev/docker-compose.aats.yml`

**实施步骤**

- heartbeat 文件写入时间戳
- 记录最近成功任务时间或最近处理 task_id
- healthcheck 检查 freshness
- 长时间 running 不推进时标记 unhealthy
- 队列 backlog 过大时给出显式异常信号

**交付物**

- 更可靠的 daemon liveness/progress 检测

**验收标准**

- daemon 卡死时容器状态转为 unhealthy
- daemon 长时间不处理任务时 health 报警

---

## 任务 7：补真实的 RDP 端到端集成测试

**优先级**：P0  
**目标**：不再依赖 README 假定它可用。

**修改范围**

- 新建 `tests/integration/data_platform/`
- 必要时补充测试 fixtures / testcontainers

**实施步骤**

- 覆盖最小生产闭环：
- trigger task -> daemon claim -> workflow finish
- approved recommendation -> gate pass -> release -> apply
- prod 拒绝 `skip_gate`
- rollback 成功
- health 在依赖异常时降级

**交付物**

- 一套可运行的 RDP 端到端测试基线

**验收标准**

- `tests/integration/data_platform/` 实际存在并可执行
- 上述核心场景可稳定跑通

---

## 任务 8：建立 staging 准生产演练流程

**优先级**：P1  
**目标**：prod 之前必须有一套真实演练环境。

**修改范围**

- staging 环境配置
- 相关 runbook / checklist

**实施步骤**

- 独立 staging DB / artifacts / env
- 完整跑通：
- `data_maintenance`
- `governance_cycle`
- `research_cycle`
- `decision_cycle`
- 至少完成一次：
- recommendation 审批
- gate
- release
- observation
- rollback 演练

**交付物**

- staging 演练记录
- prod 准入 checklist

**验收标准**

- staging 完整闭环至少成功 1 轮
- 形成书面演练结果

---

## 任务 9：自动化 apply 后观察与 rollback 评估

**优先级**：P1  
**目标**：apply 成功不等于生产安全，必须持续观察。

**修改范围**

- `aats/data_platform/production_workflow/observation_window.py`
- `aats/data_platform/production_workflow/rollback_policy.py`
- 相关 workflow / operator surface

**实施步骤**

- 自动采集 apply 后关键指标：
- `/system/health`
- reconciliation
- decision frequency
- order intent 数量变化
- execution realism 偏差
- operator 异常反馈
- 自动输出：
- continue observing
- warn
- recommend rollback

**交付物**

- 结构化 observation report
- rollback recommendation 输出

**验收标准**

- 至少一条 release 能完整跑完 observation -> evaluation

---

## 任务 10：统一文档、告警、部署、运行手册口径

**优先级**：P2  
**目标**：避免代码是一套、运维执行另一套。

**修改范围**

- `aats/data_platform/README.md`
- `docs/operations/rdp_reliability_runbook.md`
- `docs/operations/parameter_apply_and_rollback.md`
- `docs/operations/rdp_environment_matrix.md`
- `deploy/wsl2-dev/README.md`
- `deploy/wsl2-dev/RUNBOOK.md`

**实施步骤**

- 对齐实际 health 定义
- 对齐实际 apply/release 入口
- 对齐 daemon 行为
- 对齐 staging/prod 准入流程
- 对齐 `.env.wsl2` 的单一真相位置

**交付物**

- 文档、runbook、deploy 行为一致

**验收标准**

- 运维按文档执行不会踩路径或流程分叉

---

## 5. 推荐执行节奏

### 第 1 周

- 任务 1
- 任务 2
- 任务 4

### 第 2 周

- 任务 3
- 任务 5
- 任务 6

### 第 3 周

- 任务 7
- 任务 8

### 第 4 周

- 任务 9
- 任务 10

---

## 6. 阶段性放行标准

### 允许进入 staging 演练前

- 任务 1-6 全部完成
- 至少有一套基础 RDP integration 测试通过

### 允许进入 prod 试运行前

- 任务 1-10 全部完成
- staging 完整演练至少 1 轮成功
- `prod` 下 direct apply 无法绕过 gate / approval
- RDP health 能正确发现 DB / daemon / workflow 异常

---

## 7. 执行记录

### 当前状态

- [ ] 任务 1 未完成
- [ ] 任务 2 未完成
- [ ] 任务 3 未完成
- [ ] 任务 4 未完成
- [ ] 任务 5 未完成
- [ ] 任务 6 未完成
- [ ] 任务 7 未完成
- [ ] 任务 8 未完成
- [ ] 任务 9 未完成
- [ ] 任务 10 未完成

### 备注

- 本文档是整改执行总清单，不替代每个任务各自的设计文档与实现 PR。
- 如中途发现架构前提不成立，应先补充设计文档，再继续执行后续任务。
