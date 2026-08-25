# FS-022 可复现依赖与镜像供应链设计和实施范围

> 文档状态：Phase 3T Python 锁文件、哈希安装和外部镜像摘要已实施；APT、SBOM、漏洞/许可证扫描、干净镜像构建与远端治理开放  
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3V 整改；本文件主体记录 Phase 3T  
> 核对范围：Python 3.12/Linux 运行时与 CI 依赖、Dockerfile、WSL2 Compose 外部镜像、CI 安装入口、静态供应链契约与文档  
> 运行时边界：不读取 `.env.*`，不连接数据库、Redis、NATS、交易所或账户，不启动服务或 Docker，不部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段处理 `FS-022` 中能够在仓库内安全收口的可复现基础：为目标 Python 平台提交完整
依赖闭包和制品哈希，使 Docker 与 CI 不再按执行当天解析开放版本；固定 Python 基础镜像
和九个外部基础设施镜像的 manifest digest；建立自动防回退契约。

本阶段不宣称整条供应链已经可信。Debian APT 包仍由镜像构建时的软件源解析；尚未生成
和签署 SBOM，未执行 CVE、license、secret、malware 或 provenance 扫描，未在干净 Docker
daemon 构建镜像，也未运行 GitHub 远端 job。上述任一缺失都足以使 `FS-022` 保持部分整改。

## 2. 整改前行为与根因

整改前 `pyproject.toml` 的应用依赖大多使用 `>=`，Docker builder 会升级 `pip<26`，单独
安装开放范围的 `grpcio`，再解析 `.[nats,redis,otel]`。CI 同样直接解析
`.[test,lint]`。Python 基础镜像和 Compose 外部镜像只有 tag，没有 digest。

因此相同 Git commit 在不同时间可以得到不同 Python 版本闭包和不同镜像内容。根因不是
SemVer 范围本身，而是发布消费路径直接使用人审输入、没有机器生成闭包、没有 hash、
没有不可变镜像引用，也没有自动阻止这些约束被绕过。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `requirements/*.in` | 记录目标、extras 和少量必须显式固定的解析输入 |
| `requirements/*.lock` | 记录指定平台的完整第三方 distribution 闭包、精确版本和 SHA-256 |
| `requirements/README.md` | 定义目标平台、生成方法、消费方法、评审纪律和可信边界 |
| `deploy/wsl2-dev/Dockerfile` | 固定 Python 基础镜像；先按 hash 安装 wheel，再 no-deps 安装本地源码 |
| `.github/workflows/quality.yml` | 在测试前校验契约并按 CI 锁文件安装 |
| `deploy/wsl2-dev/docker-compose*.yml` | 本地 `aats-base:dev` 之外的 registry image 必须 tag + digest |
| `scripts/verify_dependency_locks.py` | 无第三方依赖地验证锁覆盖、Docker/CI 安装入口和镜像摘要白名单 |
| FS-022 contract tests | 对锁文件、基础镜像、workflow 和 Compose 防回退 |

## 4. 输入、输出与接口

人审输入：

- `runtime-py312-linux-x86_64.in`：项目基础依赖加 `nats`、`redis`、`otel` extras，并显式
  纳入 editable build 所需的 `setuptools`、`wheel` 和目标平台必需的 `greenlet`；
- `ci-py312-linux-x86_64.in`：项目基础依赖加 `test`、`lint` extras 和 `greenlet`；
- `pyproject.toml`：仍是 direct dependency 与 extra 的语义真源。

机器生成输出是两份 `.lock`。每个 requirement 必须为 `name==version`，并至少携带一个
`sha256`；禁止 URL、VCS、editable 或未固定范围进入锁文件。锁只用于 CPython 3.12、
Linux x86_64/manylinux 2.17+，不得作为 Windows 解析结果。

## 5. 数据库 schema、表、索引与约束

无数据库、migration、ORM 或持久化格式变更。依赖锁定不能证明任何 schema 已部署或当前
数据库可用。

## 6. 事务、一致性与并发

锁更新必须把 `.in`、`.lock`、镜像摘要、消费入口、测试和审计说明视为一个评审单元。
只改其中一部分会由静态 verifier 或 CI 安装失败阻断。生成操作应在隔离工作区完成；同一
更新不得并发写同一 lock。运行时交易事务、OrderState 三层状态和消息并发语义未改变。

## 7. 授权、认证与数据安全

- 锁生成和制品下载不需要读取项目 `.env.*` 或任何账户凭据；
- workflow 保持 `contents: read`，不使用 secrets、部署权限或 registry push；
- hash 只验证下载内容匹配被审阅的索引记录，不证明包安全、维护者可信或无恶意代码；
- digest 只固定 registry manifest，不证明镜像无漏洞、签名有效或运行配置安全；
- 任何后续扫描不得把 secret 值输出到日志或 artifact。

## 8. 错误处理与幂等

锁语法错误、缺失 direct dependency、未固定版本、缺失/非法 hash、基础镜像 digest 漂移、
外部 Compose image 无摘要、摘要不在已审白名单、CI/Docker 回退到开放解析，均必须非零
失败。重复校验只读仓库文件，具有幂等性。

更新镜像 tag 或 digest 必须同时更新 verifier 白名单和审计来源；不能临时放宽为“任何
64 位摘要都接受”来制造绿色。

## 9. 状态转换与生命周期

```text
review direct inputs and target
  -> isolated resolver generates complete hashed lock
  -> review dependency and digest delta
  -> verify syntax/direct coverage/approved image references
  -> verify every listed target wheel against its hash
  -> clean target install and image build
  -> SBOM + CVE/license/secret/provenance gates
  -> tests
  -> human release decision
```

Phase 3T 只完成到制品下载校验和仓库内 contract tests；后续步骤不能由前序步骤推定通过。

## 10. 缓存与性能

Docker 先复制 runtime lock 并安装第三方 wheel，再复制源码，以保留依赖 layer cache。
`--only-binary=:all:` 避免不可控的源码构建和 builder 编译工具链。CI 暂不启用 pip cache，
避免在远端锁消费尚未实跑时引入额外缓存状态。锁文件包含多个平台 wheel 的允许 hash，
体积增加是 pip hash 模式的可审计代价。

## 11. 日志、监控与审计

`scripts/verify_dependency_locks.py` 成功时只输出 runtime/CI 包数量和外部镜像数量，不打印
环境或凭据。pip 制品校验日志可记录公开 distribution 名称/版本，不应归档认证 header。
镜像 digest 来源、检查日期和目标平台写入 FS-022 审计记录。

运行时镜像 ID、SBOM、扫描报告和构建 provenance 尚不存在；文档必须继续标为 UNKNOWN，
不得用已提交 digest 替代实际构建证据。

## 12. 测试策略

新增对抗测试覆盖：

1. 两份 lock 的 package 数量、精确版本、SHA-256 格式和重复项；
2. 当前 `pyproject.toml` direct dependencies/extras 均被对应 lock 覆盖；
3. Docker 两个 Python stage 使用相同批准 digest；
4. Docker 只通过 `--require-hashes --only-binary=:all:` 安装第三方包，源码安装使用
   `--no-deps --no-build-isolation`；
5. CI 在安装前运行 verifier，并消费 CI lock；
6. 所有 Compose 外部镜像与九项已审 tag/digest 一致，本地 `aats-base:dev` 明确例外；
7. verifier 主入口可作为 CI 预安装步骤运行。

此外对两份 lock 中每个显式 package 进行 Linux x86_64/CPython 3.12 wheel 下载和 hash
校验。目标 Docker clean build、完整 CI 远端安装和 SBOM/scan 仍需后续环境证据。

## 13. 迁移、回滚与兼容

代码 API、配置字段和数据库无迁移。Docker 运行时依赖版本会从“构建当天最新兼容”变为
锁中版本；这是预期行为，但仍需 clean image regression 才能证明运行兼容。

回滚时必须整体恢复 lock、Dockerfile、workflow、Compose digest 白名单和 verifier，不能
只删除 hash 标志。若某个 pinned 制品从 registry/PyPI 暂时不可用，应视为供应链故障并
停止构建，不得自动回退到 tag 或开放版本。

## 14. 配置与环境隔离

目标固定为 CPython 3.12/Linux x86_64。Windows 本地 `.venv` 可继续用
`pip install -e ".[test]"` 做开发，但其解析结果不是发布真源。生成/验证不设置 live
profile，不加载 managed runtime，不接触 `.env.*`。Docker/CI 的 target 与本地 Python
3.14 结果必须分别记录。

## 15. 代码组织与依赖

本阶段新增 `requirements/` 作为受治理构建输入目录，不把 lock 放在 `docs/` 或临时目录。
verifier 只使用 Python 标准库，确保它能在 CI 安装任何第三方依赖前运行。运行时 source
仍通过项目 `pyproject.toml` 注册，但 `--no-deps` 禁止再次解析依赖。

当前仍保留 runtime stage 的 `apt-get`，其 Debian package 版本和 repository snapshot 未
固定。这是明确的可复现缺口，不应通过弱化 `FS-022` 定义来忽略。

## 16. 文档、运维手册与验收标准

Phase 3T 仓库内验收标准：

- runtime/CI 完整 lock 均为精确版本且每项带 SHA-256；
- 两份 lock 的目标 wheel 可全部下载并通过 hash；
- Docker/CI 不再解析开放 Python 依赖；
- Python 基础镜像和九个外部 Compose image 固定 tag + manifest digest；
- verifier、focused tests、全仓 Ruff、完整 unit、链接与 diff check 通过；
- 文档明确区分“版本/内容固定”和“漏洞/签名/来源可信”；
- 未运行的 Docker build、远端 CI、APT snapshot、SBOM/CVE/license/secret/provenance
  继续登记为开放门禁；
- 真实资金生产继续 `NO-GO`。

当前裁定：`PARTIALLY REMEDIATED / PYTHON LOCKS & IMAGE DIGESTS ADDED / APT-SBOM-CVE & CLEAN BUILD OPEN`。

实施证据见
[`../../audit/full_system_2026_08_24/40-fs-022-reproducible-dependencies.md`](../../audit/full_system_2026_08_24/40-fs-022-reproducible-dependencies.md)。
