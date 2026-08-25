# 12 性能、扩展性、依赖与构建复现审查

## FS-022 — 依赖无锁定、构建不可精确复现

- 严重度：P2；置信度：高；类别：dependency / supply chain / reproducibility
- 状态：Phase 1/2 `VERIFIED`；Phase 3T `PARTIALLY REMEDIATED / PYTHON LOCKS & IMAGE DIGESTS ADDED / APT-SBOM-CVE & CLEAN BUILD OPEN`
- 原始位置：`pyproject.toml:11-67`；原 `deploy/wsl2-dev/Dockerfile:50-58`
- 原始证据：运行依赖大多只有 `>=`，仓库没有 lockfile/constraints/hash；镜像构建会升级 `pip<26` 并安装 `.[nats,redis,otel]` 的当时最新兼容版本。Python 基础镜像/基础设施镜像也只有 tag。
- Phase 3T 证据：新增目标 Python 3.12/Linux x86_64 的 runtime/CI 完整版本和 SHA-256 lock；Docker/CI 按 hash 安装；两个 Python stage 与九个外部 Compose image 固定 manifest digest；标准库 verifier 和六项 FS-022 contract tests 防回退。APT、clean build、SBOM、CVE/license/secret/provenance、远端 CI 和独立复核仍开放，详见 `40`。

## FS-008 — PostgreSQL 连接容量与并发预算

- 原始状态：四个主 runtime pool 理论 240；全池稳态/启动理论值约 317/321，超过 PostgreSQL 200。
- Phase 2 裁定：P2 `DOWNGRADED`；overflow 按需创建，可信峰值约 142–160 但未实测。
- Phase 3U 状态：`PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN`。
- Phase 3U 证据：角色化主 pool 32/8/10/16；14 个声明 topology component 合计 150；Compose 普通容量 197、名义余量 47；13 个应用 `create_engine` 调用被 AST inventory 归类并接入 CI。
- 残余风险：transient/CLI/迁移/恢复/admin/仓库外 engine 并发未硬封顶；降低 pool 可能增加 wait/timeout；慢查询、重连峰值、目标 p95/p99、告警和联合内存均未验证。详见 `41`。
- 后果：同一 Git commit 在不同日期得到不同依赖；上游破坏、恶意包或新不兼容版本可改变资金系统行为。
- 剩余建议：固定/快照 APT 输入；在干净目标环境构建并执行 integration；生成/签署 SBOM 和 provenance；建立 CVE/license/secret/签名扫描、升级 PR 与撤包恢复流程。当前 CVE 状态为 UNKNOWN，不得写“无漏洞”。

## 性能热点

- Gateway dashboard 使用多层并发 fan-out；源码历史注释记录 137 秒 recovery view、25 秒 singleflight、15+ idle-in-transaction。原连接池扩大到 60 是局部缓解，同时制造全局容量风险；Phase 3U 已将 Gateway ceiling 降为 32，但目标排队、timeout 和 latency 尚未压测。
- async FastAPI handler 大量调用同步 SQLAlchemy/KDF；高并发会阻塞 event loop 或依赖 thread pool，需用 event-loop lag 和 DB queue wait 量化。
- Gateway 内存限制 3 GiB，源码注释估计 400–600 MiB；没有本次压测证据。
- PostgreSQL 200 connections + 64 MB work_mem 在 2.5 GiB 容器下，最坏内存远超容器限制；实际分配按算子，但配置缺少保守总预算。
- NATS file store 8 GiB，三个 streams 的静态预算约 6.5 GiB；需监控存储、consumer lag 与 purge/recovery，不应靠删除 volume 处理容量问题。
- RDP 查询和 78 表 pipeline 可能与交易控制面共享 PostgreSQL 实例资源；数据库名隔离不等于 CPU/IO/connection 隔离。

## 扩展性原则

1. 交易关键路径、operator read plane、RDP research 的连接/CPU/IO 配额分开。
2. 并发 fan-out 必须受全局 semaphore 和 deadline 控制，不能让每层独立放大。
3. 快照 API 返回 bounded payload、分页和 as_of；避免每次 mutation 全量重算。
4. 对象/事件保留策略按资金事实、可恢复状态、可丢遥测分层。
5. 所有容量值用压测和 production-like trace 校准，不以源码注释作为证明。

## 未执行/未知

- 未跑 load test、soak、profiling、EXPLAIN ANALYZE、数据库 bloat/索引命中、NATS 大 backlog 或真实 UI waterfall。
- 未做依赖 CVE/许可证/secret/provenance 扫描；Phase 3T 只在临时隔离目录安装固定 resolver 并下载公开 wheel 验证 hash，没有把扫描器写入项目环境。
- 未验证镜像最终 SBOM、非 root 权限细节、layer secrets 和 build cache provenance。
