# 27 FS-005 Gateway Loopback 与本地入口收口

> 核对日期：2026-08-24  
> Git 基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上未提交 Phase 3A–3G 叠加变更  
> 当前裁定：`CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN`  
> 上线决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 范围与前置裁定

Phase 2 将 FS-005 从 P1 降为 P2：all-interface bind 是已验证配置事实，但当时只读 HTTP 观测来自模拟 profile；live 路径还有 TLS、auth 与 Secure cookie 补偿控制。目标网络、防火墙、VPN/NAT、证书信任和远端可达性始终为 UNKNOWN。

本阶段只收口仓库默认网络暴露和本地替代入口。没有读取 `.env.*`、没有启动或重建容器、没有连接数据库/账户/交易所，也没有执行任何网络探测。

## 2. 修复前路径

1. infrastructure Compose 的所有 published port 已固定 `127.0.0.1`；
2. Gateway 应用 Compose 仍使用 `${host_port}:${container_port}`，Docker 会发布到全部宿主接口；
3. container 内 `uvicorn --host 0.0.0.0` 与宿主 published binding 在旧文档中容易被混为一谈；
4. `scripts/start_api.py` 允许 `spot_live`/`derivatives_live`，也允许显式 `--host 0.0.0.0`，可扩大裸 HTTP 本地入口；
5. Phase 3F 的模拟 evidence 没有读取实际 Gateway HostIp，不能阻断 runtime binding drift。

## 3. 实施

- Gateway Compose mapping 改为 `127.0.0.1:${AATS_API_PORT}:${AATS_API_PORT}`；container 内 listener 保持 `0.0.0.0`，供 Docker network 使用。
- `start_api.py` 的 profile choices 只保留 `spot`/`derivatives`；显式和配置解析后的 host 都必须是 IP loopback 或 `localhost`，否则在 Uvicorn 前失败。
- deployment evidence 读取 Gateway `.NetworkSettings.Ports` JSON；缺 binding、JSON/shape/port 无效或任何 HostIp 为空、all-interface、非 loopback时失败。
- evidence 只记录 `container_port`、`host_ip`、`host_port`，不含凭证；允许 `127.0.0.1`/`::1`。
- 现行项目、部署、架构、Operations、Skill 与代码审查文档统一解释 container listener 和 host publishing 的边界。

## 4. 验证

| 检查 | 结果 |
|---|---|
| FS-005/start_api/FS-007/deploy/login/process related | 首次 `2 failed, 74 passed`（旧 all-interface/文案契约）；更新断言后 `76 passed`，1 个既有 pytest cache 权限 warning |
| Ruff focused | `All checks passed` |
| 最终全量 unit | `4219 passed, 30 skipped, 1666 warnings, 85 subtests passed in 101.93s` |
| Compose YAML 与 Gateway mapping | 解析通过，唯一 Gateway mapping 以 `127.0.0.1:` 开头且含两处 `AATS_API_PORT` |
| Ruff `aats/ --fix` 与 `apps scripts tests --fix` | 均为 `All checks passed` |
| shell / PowerShell syntax | deploy、sync 与 4 个 lifecycle/deploy wrapper 通过 |
| 变更 Markdown 相对链接 | 62 个文件通过 |
| Git whitespace/diff | `git diff --check` 通过；仅有既有 LF/CRLF checkout 提示 |

focused 覆盖 Compose 精确 mapping、loopback/非 loopback 地址、live parser 拒绝、evidence loopback 接受、空/all-interface/非 loopback/malformed binding 拒绝，以及 FS-007/登录文档兼容。第一次扩大回归的两个失败均是旧测试仍要求两段式 all-interface mapping 或旧文案；安全契约更新后同组通过。第一次 YAML 命令因 PowerShell 展开 `$` 且第二次因把 Compose 变量中的 `:-` 误计作分隔冒号而失败；改用不含环境变量插值歧义的结构断言后通过。这些失败没有被写成产品代码成功，也没有被省略。

全量 warnings 与前阶段一致，主要是 SQLite datetime deprecation、LongShort poller AsyncMock 未 await 和 pytest cache 权限 warning。本阶段未执行 WSL2 integration。

## 5. 已验证与未知

### 静态/隔离已验证

- 新建 Gateway 容器时，Compose 目标宿主 mapping 是 IPv4 loopback；
- 本地 launcher 不能通过 live profile 或非 loopback host 启动；
- 标准模拟部署在 evidence 阶段会拒绝实际 Gateway binding drift；
- container 内部 listener 仍能通过 Docker network 接收流量；
- 文档不再把静态 loopback 等同于目标网络已安全。

### 运行时未验证/UNKNOWN

- 当前任何既有容器是否已经按新 mapping 重建；
- Windows/WSL/Docker 实际 HostIp 与端口转发行为；
- 宿主防火墙、LAN、VPN、NAT、公网和非授权网络可达性；
- TLS 证书 SAN/信任、HTTP 强制、TrustedHost、认证/限流与 cookie 的目标环境组合；
- 受控远程 Operator 接入架构。

## 6. 当前裁定

FS-005 的仓库代码路径已收口，但不能关闭目标环境验证：

```text
CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN
```

G7 仍为 `PARTIAL / 未放行`。loopback 是纵深防御，不是生产网络证明；当前 live 继续由 Phase 3F 硬禁用。

## 7. 最终关闭条件

1. 在隔离目标主机重建模拟 Gateway，保存 Docker inspect published HostIp 与 evidence；
2. 从本机、LAN、VPN 外和非授权网络探测允许/拒绝矩阵；
3. 验证 HTTPS 强制、证书 SAN/信任、HTTP login 拒绝、Host、auth、cookie 与 rate limit；
4. 需要远程访问时通过另行批准的 proxy/VPN/mTLS，而不是放宽本 Compose；
5. 由独立 reviewer 复核静态、隔离运行和目标网络证据。

本阶段不构成部署、远程暴露、live 解禁或真实资金授权。
