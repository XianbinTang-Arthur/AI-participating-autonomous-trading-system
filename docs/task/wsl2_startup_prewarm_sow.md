# WSL2 开机保活与预热任务书

## 业务目标与边界

- 目标：Windows 登录后自动建立一个 Windows 侧长期存活的 WSL keepalive 进程，确保 `Ubuntu` 不会在预热脚本退出后立刻回到 `Stopped`。
- 目标：在 keepalive 已建立的前提下，再执行 WSL 唤醒、Docker 就绪检查、AATS 栈健康检查，以及必要时的一次标准 repair deploy。
- 目标：避免登录页“能打开，但提交登录时因为 WSL 又掉回 stopped 而直接 Failed to fetch”的冷启动假故障。
- 边界：不引入第二套部署体系；如需恢复仍必须复用仓库唯一入口 `scripts/deploy.sh`。
- 边界：不自动同步 Windows 未提交代码，不自动提交代码，不读取或打印 `.env.wsl2` / `.env.*` 凭证内容。

## 模块职责与领域模型

- `scripts/keepalive_wsl2_aats.ps1`
  - 负责 Windows 侧 WSL keepalive 进程的启动、停止、状态检查。
  - keepalive 本质是一个隐藏的 `wsl.exe` 长驻进程，在 `Ubuntu` 中执行低开销的无限休眠循环。
  - 通过本地状态文件和 Windows 进程命令行哨兵实现幂等识别。
- `scripts/prewarm_wsl2_aats.ps1`
  - 负责登录后启动保活、唤醒 WSL、等待 Docker、检查 AATS 栈健康。
  - 如现有栈未恢复成功，则调用标准 deploy 包装器执行一次 `-SkipSync -SkipCommit` repair deploy。
- `scripts/register_wsl2_aats_startup_task.ps1`
  - 负责注册或移除登录计划任务。
  - 计划任务只挂 `prewarm_wsl2_aats.ps1`，由 prewarm 自己确保 keepalive 已建立。

## 输入 / 输出接口

### `keepalive_wsl2_aats.ps1`

- 输入：
  - `-Action`：`Start | Stop | Status`
  - `-Profile`
  - `-Distro`
  - `-DryRun`
- 输出：
  - 标准输出打印 keepalive 状态、启动结果、停止结果
  - `Status` 时返回当前是否已有有效 keepalive 进程

### `prewarm_wsl2_aats.ps1`

- 输入：
  - `-Profile`
  - `-Distro`
  - `-DockerTimeoutSeconds`
  - `-HealthTimeoutSeconds`
  - `-DeployTimeoutSeconds`
  - `-SkipRepairDeploy`
  - `-SkipKeepAlive`
  - `-DryRun`
- 输出：
  - 标准输出打印阶段性状态
  - 非零退出码表示 keepalive / WSL / Docker / AATS 恢复失败

### `register_wsl2_aats_startup_task.ps1`

- 输入：
  - `-Profile`
  - `-TaskName`
  - `-DelaySeconds`
  - `-Remove`
  - `-DryRun`
- 输出：
  - 注册或删除登录计划任务
  - 标准输出打印任务名称、脚本路径、触发参数

## 数据库 / 表 / 索引 / 约束

- 本任务不修改数据库 schema。
- 不新增表、索引、约束。

## 事务、一致性与并发

- keepalive 脚本不直接写数据库。
- repair deploy 继续复用 `scripts/deploy.sh`，由现有部署流程负责容器停启一致性。
- keepalive 启动必须幂等：
  - 如果已有有效 keepalive，则复用，不重复拉起第二个长驻进程。
  - 如果状态文件已过期，但进程仍在，则重建状态文件。
  - 如果状态文件存在但进程无效，则清理并重建。

## 鉴权、认证与数据安全

- 不读取或输出任何凭证内容。
- keepalive 状态文件只保存 PID、发行版、时间戳、命令哨兵，不保存密钥、口令或数据库 URL。
- 登录页网络错误本地化只改变前端提示，不改变认证接口语义。

## 错误处理与幂等

- `keepalive_wsl2_aats.ps1`
  - `Start`：已有有效 keepalive 时直接返回成功。
  - `Stop`：没有 keepalive 时直接返回成功。
  - `Status`：无 keepalive 时返回非抛异常的状态输出。
- `prewarm_wsl2_aats.ps1`
  - keepalive 启动失败时直接终止。
  - Docker 未就绪时轮询至超时。
  - AATS 栈不健康时最多触发一次标准 repair deploy。
- 登录页：
  - 网络层 `Failed to fetch` 必须显示为明确中文，提示服务未启动或接口不可达。

## 状态迁移与生命周期

1. Windows 用户登录。
2. 计划任务触发 `prewarm_wsl2_aats.ps1`。
3. prewarm 先调用 `keepalive_wsl2_aats.ps1 -Action Start`。
4. keepalive 建立长期存活的 `wsl.exe` 进程，保持 `Ubuntu` 处于 `Running`。
5. prewarm 再唤醒 WSL、等待 Docker ready。
6. prewarm 检查所需容器和 gateway `/healthz`。
7. 若未恢复，则执行一次标准 repair deploy。
8. repair 完成后再次检查健康。
9. prewarm 退出，但 keepalive 继续保活。

## 缓存与性能

- keepalive 长驻命令仅执行低开销休眠循环。
- 预热仍只轮询：
  - `docker info`
  - `docker inspect`
  - `GET /healthz`
- 不增加高频轮询或额外后台服务。

## 日志、监控、审计

- `keepalive_wsl2_aats.ps1` 和 `prewarm_wsl2_aats.ps1` 都使用统一前缀日志，便于在计划任务历史中排查。
- repair deploy 如触发，继续复用现有 deploy 日志。

## 测试策略

- 单元测试：
  - keepalive 脚本必须包含隐藏启动 `wsl.exe`、长驻循环、状态文件和进程校验逻辑。
  - prewarm 必须引用 keepalive 脚本并在健康检查前调用 `Start`。
  - 注册脚本必须仍创建 `AtLogOn` 任务，并说明 keepalive + prewarm 语义。
  - 登录脚本必须把网络错误翻译成明确中文。
- Dry-run：
  - `keepalive_wsl2_aats.ps1 -DryRun`
  - `prewarm_wsl2_aats.ps1 -DryRun`
  - `register_wsl2_aats_startup_task.ps1 -DryRun`
- 窄集成验证：
  - 登录页路由仍可正常返回。

## 迁移、回滚与兼容性

- 新增 keepalive 脚本为增量能力，不改变现有手工 deploy 流程。
- 回滚时可删除计划任务，停止 keepalive 进程，并回退脚本。
- 继续兼容现有 profile 和 `Ubuntu` 默认发行版。

## 配置与环境隔离

- 默认 profile 仍为 `derivatives-live`。
- 默认发行版仍为 `Ubuntu`。
- 不改 `.env.wsl2` 与 `.env.derivatives.live` 的既有位置约定。

## 代码组织与依赖

- Windows 侧脚本放在 `scripts/`
- 文档放在 `docs/task/` 与 `docs/operations/`
- 不引入新的 Python 或 Node 依赖

## 文档与运维手册

- 运维文档需新增：
  - 如何手工查询 keepalive 状态
  - 如何手工启动或停止 keepalive
  - 如何重新注册登录任务
  - 登录页出现“登录接口不可达”时优先排查什么

## 部署与验收标准

- `keepalive_wsl2_aats.ps1 -DryRun` 成功输出预期动作
- `prewarm_wsl2_aats.ps1 -DryRun` 成功输出 keepalive + prewarm 动作
- `register_wsl2_aats_startup_task.ps1 -DryRun` 成功输出任务配置
- 单元测试覆盖关键静态行为
- 登录页网络错误文案本地化后，不再显示原始 `Failed to fetch`
- 代码发布仍通过标准入口 `bash scripts/deploy.sh --skip-commit`
