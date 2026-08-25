# FS-005 Gateway 本机绑定与本地入口收口 SOW

> 文档状态：现行实施约束  
> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：Phase 3A–3F 未提交叠加变更  
> 目标裁定：`CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 背景与问题定义

FS-005 的 Phase 2 证据确认：`deploy/wsl2-dev/docker-compose.aats.yml` 中 Gateway 使用 `host:container` 端口映射，Docker 将其发布到所有宿主接口。当前模拟运行快照曾观察到 `0.0.0.0`/`[::]`，扩大了登录与 Operator 控制面的网络攻击面。

基础设施 Compose 的 Postgres、Redis、NATS、Grafana、Prometheus、Loki、Jaeger 等端口已经显式绑定 `127.0.0.1`，Gateway 是现行 WSL2 栈中的不一致例外。另有本地 `scripts/start_api.py` 允许选择 live profile 和通过 `--host 0.0.0.0` 扩大裸 HTTP 绑定，形成绕过“本地模拟入口”语义的替代路径。

## 2. 目标与非目标

目标：

1. Gateway 宿主端口静态限定到 IPv4 loopback；
2. 本地 API 启动器只接受 `spot`/`derivatives` 模拟 profile；
3. 本地 API 启动器拒绝非 loopback host；
4. 模拟部署 evidence 校验并记录 Gateway 实际 published binding；
5. 现行文档明确远程访问必须使用另行受控的 proxy/VPN/mTLS 设计；
6. 以静态/隔离测试证明配置与入口失败关闭。

非目标：不修改容器内部 `uvicorn --host 0.0.0.0`，因为容器内需要通过 Docker network 接收连接；不配置远程代理、防火墙、VPN、证书 PKI 或公网入口；不部署、不探测局域网、不连接真实账户或交易所。

## 3. 用户与操作场景

- 本地开发者通过 `http://127.0.0.1:<port>` 使用模拟 Gateway；
- 标准 WSL2 模拟部署只能把 Gateway 发布到宿主 loopback；
- 操作员若误传 live profile 或 `--host 0.0.0.0` 给本地启动器，应在应用启动前得到明确失败；
- 未来确需远程运维时，必须新建设计和授权任务，使用受控代理/VPN/mTLS，不通过放宽本 Compose 映射实现。

## 4. 需求与验收标准

1. Compose Gateway 端口必须是 `127.0.0.1:host:container` 三段式映射；
2. `start_api.py --profile` choices 只能是两个模拟 profile；
3. `apply_runtime_bind_overrides` 对显式非 loopback host 抛出错误，且不得写入环境；
4. deployment evidence 至少存在一个 Gateway published binding，且每个 `HostIp` 都是 loopback；
5. 任一 all-interface、空 HostIp 或非 loopback binding 必须让 evidence 生成失败；
6. 文档不得把 loopback 代码修改写成目标宿主防火墙、NAT、VPN 或生产证书已经验证；
7. focused/相关测试、Ruff、Compose 静态解析或等价 YAML 检查、Markdown 链接和 diff check 通过。

## 5. 安全与资金边界

这是控制面纵深防御修改，不授权 live。不得读取 `.env.*`、启动 Compose、访问账户/交易所、提交订单或修改数据库。当前 live 仍由 Phase 3F 硬禁用。

Loopback 映射只减少默认网络暴露，不替代认证、TLS、Host 校验、限流、防火墙或独立网络分区。运行态网络继续按 UNKNOWN 处理。

## 6. 现状与真相源

真相源优先级：Compose 端口声明与 `start_api.py` > 部署脚本/evidence writer > 当前单元测试 > 现行部署/运维文档 > 历史运行记录。

已核对：基础设施所有 published ports 均为 `127.0.0.1`；应用 Compose 只有 Gateway 发布宿主端口，当前缺少 loopback 前缀；container 内部 listener 与宿主 published binding 是两个不同边界。

## 7. 方案设计

- 将 Gateway mapping 改为 `127.0.0.1:${AATS_API_PORT}:${AATS_API_PORT}`；
- `start_api.py` 的 profile choices 删除 live，新增小型 loopback validator；
- evidence writer 对 `aats-gateway` 读取 Docker `.NetworkSettings.Ports` JSON，规范化为无凭证记录；
- 只接受 `127.0.0.1` 或 `::1`，拒绝 Docker inspect 中的空/all-interface/非 loopback HostIp；
- 保持 container command 的 `--host 0.0.0.0`，避免破坏 Docker network 内部连通性。

## 8. 数据与 API 契约

不改业务 API、数据库或事件契约。部署 evidence 新增 `gateway_published_bindings` 数组，每项仅含 `container_port`、`host_ip`、`host_port`，不含凭证或 URL query。

`start_api.py` CLI 是有意安全收紧：live choice 与非 loopback host 从“可接受”变为启动前错误。

## 9. 控制流与失败语义

```text
local start_api
  -> parse profile: live/unknown rejected
  -> validate explicit host: non-loopback rejected before env mutation
  -> load simulation profile and start

simulation deploy evidence
  -> inspect required containers
  -> inspect gateway published ports
  -> missing/malformed/non-loopback binding: fail, no success evidence
  -> loopback binding: record facts, continue
```

## 10. 性能与容量

只新增一次 Gateway `docker inspect`，不在交易热路径。证据大小增加固定数量端口记录。loopback mapping 不改变容器内请求处理能力。

## 11. 日志、监控与审计

错误必须说明本地 launcher 或 deployment evidence 拒绝非 loopback binding，但不得打印凭证。evidence 记录真实 HostIp/HostPort 作为本次模拟部署身份的一部分。

目标宿主防火墙、路由与外部不可达性仍需未来只读探测报告，不得由静态 YAML 自动推断。

## 12. 测试策略

新增/更新测试覆盖：

1. Compose Gateway mapping 精确 loopback，且没有旧两段式映射；
2. `start_api.py` 接受 loopback、拒绝 `0.0.0.0`/`::`/非本机地址且拒绝 live profile；
3. evidence 记录 loopback published binding；
4. evidence 对空/all-interface/non-loopback/malformed binding 失败；
5. FS-007/部署/登录文档回归保持通过；
6. Ruff、相关单测、全量 unit 和文档检查。

不执行运行态 Docker inspect；使用无凭证替身 JSON 验证解析与失败语义。

## 13. 迁移、回滚与兼容

重新创建模拟 Gateway 容器后端口将只在本机可达；依赖从 LAN 直接访问开发 Gateway 的非标准流程会中断，这是预期安全变化。需要远程访问者不能回滚到 all-interface，而应提出受控接入设计。

代码回滚只允许在本地测试证明必要且不重新扩大暴露面时评估；当前生产 NO-GO，不存在 live 回滚授权。

## 14. 配置与环境隔离

不新增环境变量来覆盖 host binding，避免形成隐蔽 all-interface escape hatch。端口仍由 profile 的 `AATS_API_PORT` 决定，host 固定 loopback。

本地 launcher 仍允许显式 loopback host/port，便于测试不同本机端口；live profile 保留在核心配置模型中供未来受控验证，但不允许通过本地裸 HTTP launcher 启动。

## 15. 代码组织与依赖

预计修改：

- `deploy/wsl2-dev/docker-compose.aats.yml`；
- `scripts/start_api.py`；
- `scripts/write_deployment_evidence.py`；
- FS-005/FS-007 与现有启动测试；
- 部署、文档地图、Operations 与审计状态。

不新增第三方依赖；JSON/IP 校验使用标准库。

## 16. 文档与最终关闭边界

本阶段完成后，FS-005 目标状态为：

```text
CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN
```

这表示仓库默认 Gateway 暴露和本地替代入口已失败关闭；不表示现有容器已重建，也不证明 Windows/WSL/Docker 实际 HostIp、防火墙、VPN、NAT、TLS 证书信任、Host/auth/cookie/限流为安全。只有在隔离目标主机上完成本机/LAN/VPN 外探测和独立复核后，才能关闭 FS-005；真实资金继续 NO-GO。
