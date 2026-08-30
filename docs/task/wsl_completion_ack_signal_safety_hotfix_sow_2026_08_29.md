# 标准部署 WSL completion ACK 信号安全修复 SOW

> 状态：信号修复与隔离回归完成；独立计划任务重现已定位 stdin 截断根因，
> transport 修复与运行验收进行中
> 核对日期：2026-08-29
> 起始基线：`ae8ba038e249ce6a4df3303691c2d32e46fd9c6d`

## 现场问题

标准 derivatives 模拟部署的一次 schema one-off 容器以 `exitCode=0` 完成并销毁，正确的
`aats_derivatives` / `aats_research` 账本、checksum 和物理结构也全部符合当前代码；但 WSL
completion 文件没有发布，sequence 15 active marker 因而按 fail-closed 契约留存，应用未启动。

与首次现场状态一致、且已由隔离信号故障注入复现的一种失败机制位于远端 completion wrapper：同一个
destructive cleanup trap 同时绑定
`EXIT/HUP/INT/TERM`。wrapper 等待同步子命令时收到信号，Bash 会在子命令返回后先执行 trap，删除
stdout/stderr 暂存文件，随后摘要计算失败，造成“远端 mutation 已结束但 completion ACK 缺失”。
现场没有保留下来的信号遥测，因此不把信号来源或历史触发链写成已证事实。该修复提交后又有两次
标准部署在 schema one-off 正常退出后缺失 completion，其中一次由脱离当前对话的隐藏 PowerShell
启动；这否定了“已由该信号修复关闭现场根因”以及“仅是当前对话前台超时”的判断。随后使用独立
Windows 计划任务重现，阶段诊断固定为 `hash_output/status=0`。隔离探针在显式设置
`MSYS_NO_PATHCONV=1` 后，只让远端命令读取 stdin，便复现相同阶段、状态和 completion 缺失；因此当前
主根因是 encoded wrapper 通过管道喂给 Bash，远端 Docker/Compose 子命令可消费尚未执行的 wrapper
尾部。MSYS 参数改写不是该探针的必要条件，但仍属于 opaque base64 argv 的潜在破坏面。

## 目标与边界

- HUP/INT/TERM 只记录第一个 transport 信号，不提前删除暂存文件；
- 同步子命令返回后仍计算摘要并以硬链接原子发布七字段 ACK；
- ACK 与本地捕获字节完全匹配时允许清除 active marker，但 transport 非零仍令本次部署失败；
- ACK 缺失、畸形或字节不匹配继续保留 marker，禁止猜测完成；
- 不修改 profile、Compose 拓扑、schema、NATS、凭证来源或 live 门禁；
- SIGKILL 等无法运行 handler 的情形继续依赖 durable marker 与人工恢复，不宣称已解决。

## 实施

1. completion wrapper 将临时文件删除限制为 `EXIT` cleanup；HUP/INT/TERM 记录
   `129/130/143`，在 ACK 发布和输出回放后再传播 transport 状态。
2. command guard 在 WSL transport 非零时先计算捕获摘要并验证既有 ACK。有效 ACK 只消除
   mutation 是否结束的歧义；部署统一返回状态 16，不把 transport 中断伪装成成功。
3. Windows Git Bash → WSL 隔离 smoke 让同步 child 在运行中向 wrapper 发送 TERM，验证输出、ACK、
   marker 清理、锁继续持有以及部署非零语义；测试只使用专用锁和临时文件。
4. completion wrapper 的失败路径只输出允许列表阶段名和数值状态；guard 在 ACK 缺失或校验失败时
   输出 transport 状态。诊断不包含远端命令、捕获输出、路径内容或凭证，也不作为完成证明。
5. WSL transport 先校验并解码 wrapper，再以 `bash -c` 参数执行，stdin 固定为 `/dev/null`；远端命令
   即使读取 stdin，也不能再吞掉 ACK 程序。真正的 supervised `wsl` 调用同时设置
   `MSYS_NO_PATHCONV=1`，避免 Git Bash/MSYS 改写 opaque base64 参数；解码失败必须返回非零。

## 验收

- `bash -n` 通过 deploy 与 smoke 脚本；
- 聚焦单元契约通过；
- Windows Git Bash → WSL 完整锁 smoke 通过；
- ACK `preflight/status=126` 阶段诊断与 guard `transport_status=1` 诊断 smoke 通过；
- 新增 stdin-consuming remote command 回归：必须保留输出、真实退出码、completion/marker 语义；
- 全量单元、标准模拟部署与部署后健康/连续性证据在本任务后续执行并记录；
- 不 push，不启动任何 live profile，不读取或输出凭证。
