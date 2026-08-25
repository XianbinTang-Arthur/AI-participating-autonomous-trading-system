# Phase 3W：审计后全量变更复审与防御性收口

> 文档状态：代码复审与 Windows 隔离验证完成；WSL2 集成、Compose 模拟栈及持续运行观测待标准部署后验证  
> 日期：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 分支：`codex/fs-002-kill-switch-p0`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 范围与方法

本轮重新盘点起始基线以来的全部 tracked diff 与未跟踪候选文件，并从入口、配置、进程拓扑、
鉴权、Kill Switch、执行 fence、数据库迁移/连接预算、研究选择、回测成交、部署与测试门禁逐层
追踪。历史审计结论只用于定位风险，不替代当前代码、迁移、Compose、生成器和测试复核。

本轮未读取或输出 `.env.*` 内容，未启用任何 live profile，未发送订单，未把单元测试或静态
检查写成运行时正常。

## 2. 新确认并修复的问题

### 2.1 登录并发保护接受非有限配置

`AATS_LOGIN_BLOCKING_QUEUE_TIMEOUT_SECONDS` 和登录 rate-limit window 原先只校验 `> 0`。
`NaN` 可绕过该比较并进入 `asyncio.wait_for`、限流窗口和时间计算。当前 Settings 同时要求有限
且为正；`NaN`、正负无穷、零和负数全部在配置加载期拒绝。

### 2.2 Kill Switch 权威时间戳接受 `NaN`/无穷

authority、submission authority、permission lease 和远端事件的 `set_at`/`issued_at` 原先可把
非有限浮点带入代次单调性、恢复匹配和许可续租。当前所有权威时间入口统一要求有限正数；
畸形记录失败关闭并进入 degraded/halt 语义，不能铸造在线增险许可。

### 2.3 OHLCV FillSimulator 可接收非有限数值

成交模型的 participation、fee、slippage、概率及市场输入原先缺少一致的有限性校验，可能抛出
Decimal 比较异常或生成 `NaN` 证据。当前模型构造拒绝非有限/越界参数；非有限或非正市场
quantity、price、volume 统一返回 no-fill，不产生不可审计成交。

### 2.4 本地“单进程”入口实际只构建 Gateway slice

`scripts/start_api.py` 的现行文档声明为本地单进程入口，但 FastAPI 默认 role 是 `gateway`，启动
器未注入 `monolith`。结果可能表现为 HTTP 存活而缺失 market/decision/execution slice。当前
启动器在加载模拟 profile 后强制 `AATS_PROCESS_ROLE=monolith`；live 与非 loopback 拒绝不变。

### 2.5 CI warning allowlist 使用了错误的命令行语义

Quality workflow 把命令行 `-W` 的 message 写成 `3\.12.*` 正则；该参数按字面前缀处理，因而
无法忽略目标 SQLite datetime adapter 告警，严格 CI 会误失败。当前使用无正则转义的精确前缀，
并增加契约测试阻止回退。

### 2.6 SQLite 测试资源未确定性释放

多组内存 SQLite helper 只关闭 Session、未 `dispose()` Engine。Python 3.13+ 会产生 unclosed
database `ResourceWarning`，严格 warning budget 因此不稳定。当前由 `unittest.addCleanup` 或
pytest finalizer 持有并释放 engine；不是用 warning 屏蔽掩盖泄漏。

### 2.7 Compose 内嵌注释仍诱导手工运维

当前 Compose 文件头仍保留手工 `docker compose up/down/down -v` 命令，与唯一标准部署入口及
持久化卷安全约束冲突。当前注释统一改为 `scripts/deploy.sh` 编排边界；三个 live override 明确
标为当前硬门禁下的未来配置，不是可执行 runbook。

## 3. 验证证据

- Ruff 全仓：PASS；
- dependency lock：`runtime=46`、`ci=33`、external images `9`，PASS；
- database connection budget：declared ceiling `150`、operational reserve `47`、components `14`、
  engine calls `13`，PASS；
- 全量 unit（strict markers、warning error、SQLite 兼容 allowlist）：
  `4423 passed, 30 skipped, 94 subtests passed`；
- managed config artifacts：生成成功；
- `git diff --check`：无 whitespace error；CRLF 提示属于 Windows checkout 转换提示。

## 4. 当前裁定与未验证边界

本轮静态复审与 Windows 隔离测试范围内，没有保留已确认而未修复的新缺陷。该结论不覆盖待执行
的 WSL2 Postgres 集成、标准 derivatives 模拟部署、真实 NATS/Redis 四进程启动、后台日志、
系统恢复、模拟交易所只读状态和持续运行观测；这些项目必须在提交后的标准部署中单独取证。

即使模拟栈全部 healthy，FS-002 目标分区、FS-001 真实参数激活、生产等价负载、最终 OOS、
远端 required check、供应链扫描和独立复核仍未关闭，因此本记录不改变生产 **NO-GO**。
