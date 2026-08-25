# Phase 3 全量变更复审、修复与模拟盘运行验证 SOW

> 文档状态：现行实施约束  
> 日期：2026-08-25  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：Phase 3A–3V 未提交叠加变更  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本轮对上述基线以来的全部已跟踪与未跟踪代码、配置、迁移、测试、部署脚本和现行文档进行
复审；修复确认的问题，在静态与隔离回归通过后提交完整本地工作区，并通过标准部署入口启动
`derivatives` 模拟栈，观察组件健康、系统健康、恢复状态和后台日志。

本轮不启用 `spot-live`、`derivatives-live` 或 `derivatives-live-monolith`，不触发真实资金操作，
不读取或输出任何 `.env.*` 凭证。模拟栈启动成功也不等价于 trading-ready 或生产放行。

## 2. 模块责任与输入输出

- 复审输入：起始基线到当前工作区的 Git diff、未跟踪源码、配置、迁移、测试与文档；
- 修复输出：只修改复审确认存在缺陷的路径，并增加最窄回归测试；
- 交付输出：单一审计提交、测试证据、标准 `derivatives` 部署证据和运行观测结论；
- 真源：当前代码、迁移、配置生成器、Compose、部署脚本和测试，而非历史完成声明。

## 3. 数据库 Schema、事务与并发

本轮不另行设计业务表。任何 schema 变更必须继续经过 root migration ledger 和 RDP Batch B
ledger，应用启动只读校验。复审必须检查事务边界、advisory lock、checksum、连接池预算和
并发 worker/task 生命周期，不允许恢复应用启动时隐式 DDL。

## 4. 鉴权与安全

重点复核 Operator 登录 KDF 异步隔离、限流/capacity、输入长度、Host 与安全响应头、Kill
Switch 权威状态和最终交易提交 fence。所有 timeout/window 配置及权威状态时间戳必须是
有限正数；`NaN`、正负无穷、零和负数必须在配置加载或权威记录解析期失败关闭，不能进入
事件循环、限流计算、代次单调性判断或短时许可续租。回测成交模型配置也必须拒绝非有限
值；非有限市场输入按模块契约返回 no-fill，不得产生 `NaN` 成交或证据。

## 5. 错误、幂等与状态生命周期

停机、恢复、迁移、部署和配置 apply/rollback 不得把部分成功表述为完整成功。重复请求必须
保持幂等或明确拒绝；关键后台 task 异常结束或进度超时必须使健康状态失败。部署步骤任一
失败即停止，不以容器存在代替健康、恢复或交易许可证明。

## 6. 缓存与性能

复核 Redis authority/readiness generation、短时交易许可、连接池声明预算和 Operator 登录
worker 上限。静态预算不是负载结论；本轮只在本地模拟栈观察明显的连接耗尽、任务停滞、
重启循环、日志风暴与数据陈旧，不宣称完成生产容量验收。
测试使用的 SQLite engine 必须由用例 cleanup/fixture 确定性 `dispose()`；CI warning allowlist
必须使用命令行 `-W` 的字面前缀语义，不能误写成不会匹配的正则转义。

## 7. 日志、审计与秘密处理

日志和错误只保留状态、代次、组件、异常类型与非秘密元数据，不输出密码、token、cookie、
数据库 URL 或 `.env.*` 内容。运行观测需要区分 Gateway `/healthz`、`/system/health`、
`/system/recovery`、容器 health、模拟交易所状态和最新恢复快照。

## 8. 测试与验收

1. 对每项修复运行最窄单元测试；
2. 运行 Ruff、全量 `tests/unit/`、静态 verifier、secret scan 与文档/配置契约；
3. 运行受影响的最窄 WSL2 集成测试；
4. 精确暂存全部本地文件，复核 staged diff 后创建一个本地提交；
5. 使用 `bash scripts/deploy.sh --profile derivatives --skip-commit` 部署已提交 HEAD；
6. 观察必需容器、Gateway、系统健康/恢复、模拟交易所状态、数据库恢复快照及后台日志；
7. 若运行期发现问题，回到修复、回归、复审、补充提交并重新部署，直到无已确认未修复问题。

## 9. 迁移、回滚与兼容

修复保持现有公共 API 和数据格式，除非当前行为本身是不安全的错误成功。数据库迁移必须可
重复验证且由 ledger 约束。Git 回滚使用本轮提交的普通反向提交，不覆盖用户历史；运行回滚
仍走标准部署入口。不会用 `git reset --hard`、手工 Compose 或 rsync。

## 10. 配置与环境隔离

Windows 验证使用 `.venv\Scripts\python.exe`；WSL2 使用项目标准同步与部署脚本。只选择
`derivatives` 模拟 profile。配置测试通过模型/生成器加载，不读取或打印真实 env 文件内容。
本地 `start_api.py` 必须显式选择 `monolith`，避免文档声明的单进程入口实际只构建 Gateway
slice；WSL2 标准部署继续使用四主进程与 RDP daemon，不受该本地入口约束。

## 11. 代码组织与依赖

修复优先落在现有模块和对应测试中，不做无关重构。依赖继续服从 Python 3.12/Linux hash lock
与镜像 digest 契约；若 clean build 或 runtime 暴露锁文件/平台问题，必须修复锁或构建路径，
不能退回开放版本解析。

## 12. 文档、运维与最终裁定

本 SOW 位于 `docs/task/`，记录复审实施边界；现行运行说明仍以 `docs/operations/`、
`DEPLOYMENT.md` 和代码为准。最终报告必须逐项列出实际修改、残余风险、已运行测试及结果、
提交 SHA、模拟栈实测状态和仍未知事项。除非 live gate、生产等价负载、独立复核和全部未知项
另行关闭，生产决定保持 **NO-GO**。
