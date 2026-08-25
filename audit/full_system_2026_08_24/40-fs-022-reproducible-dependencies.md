# FS-022 可复现依赖与镜像摘要整改证据

> 文档状态：Phase 3T Python 锁、哈希安装和外部镜像摘要已实施；APT、SBOM/扫描、clean build 与远端治理开放  
> 最后核对：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上未提交 Phase 3A–3V 叠加变更；本记录主体证据止于 Phase 3T  
> 运行时边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动服务或 Docker，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 整改前事实

原始审计确认运行时与 CI 都直接从 `pyproject.toml` 的开放范围解析依赖，Dockerfile 还会
升级 pip、另行开放安装 grpcio。`python:3.12-slim` 以及 Postgres、Redis、NATS 和全部
observability image 只使用可变 tag。相同 Git commit 无法约束未来解析出的 distribution
版本或 registry manifest，`FS-022` 的 finding 成立。

## 2. 本阶段实现

### 2.1 目标平台 Python 锁

新增 `requirements/`：

- runtime input/lock：CPython 3.12、Linux x86_64，包含项目基础依赖、nats、redis、otel
  以及 editable build 工具，共 46 个精确 distribution；
- CI input/lock：同一目标，包含项目基础依赖、test、lint，共 33 个精确 distribution；
- 两份 lock 的每个条目均为 `==`，并携带 PyPI archive 的 SHA-256；
- 锁由固定 `uv==0.12.5` 按 `x86_64-manylinux_2_17` 解析，命令和更新纪律记录在
  `requirements/README.md`。

`greenlet==3.2.5` 被显式写入 input，避免不同索引可见性导致解析漂移。该版本选择只解决
当前目标 wheel 可获得性，不构成长期安全或兼容背书。

### 2.2 Docker 与 CI 消费路径

Docker builder 先建立 venv，再使用：

```text
pip install --require-hashes --only-binary=:all: -r runtime lock
pip install --no-deps --no-build-isolation -e .
```

原 `pip install --upgrade "pip<26"`、开放 grpcio 和 `.[nats,redis,otel]` 解析路径已删除，
builder 不再安装编译工具链。两个 Docker stage 的 `python:3.12-slim` 均固定到同一个
manifest digest。

CI 在任何第三方安装前运行标准库 verifier，再以相同 hash/binary 约束消费 CI lock。
这不证明 workflow 已在 GitHub 远端执行，只证明仓库定义不再按当日开放解析。

### 2.3 外部镜像摘要

`deploy/wsl2-dev/docker-compose.yml` 的九个外部 image 均保留可读 tag，并追加经 registry
manifest API 核验的 digest：

| Image tag | Manifest digest |
|---|---|
| `postgres:16-alpine` | `sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685` |
| `redis:7-alpine` | `sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf` |
| `oliver006/redis_exporter:v1.58.0-alpine` | `sha256:f8b9ce3393afb619696f43e000c93369258109b0ea82a37ba4d29d000c277f2f` |
| `nats:2.10-alpine` | `sha256:b83efabe3e7def1e0a4a31ec6e078999bb17c80363f881df35edc70fcb6bb927` |
| `grafana/loki:3.0.0` | `sha256:757b5fadf816a1396f1fea598152947421fa49cb8b2db1ddd2a6e30fae003253` |
| `jaegertracing/all-in-one:1.57` | `sha256:8f165334f418ca53691ce358c19b4244226ed35c5d18408c5acf305af2065fb9` |
| `prom/prometheus:v2.51.0` | `sha256:5ccad477d0057e62a7cd1981ffcc43785ac10c5a35522dc207466ff7e7ec845f` |
| `grafana/grafana:12.4.3` | `sha256:2e986801428cd689c2358605289c90ab37d2b39e24808874971f54c99bcdc412` |
| `grafana/promtail:3.0.0` | `sha256:d3de3da9431cfbe74a6a94555050df5257f357e827be8e63f8998d509c37af8b` |

本地构建的 `aats-base:dev` 没有 registry manifest，是显式例外；部署 evidence 仍应记录
实际不可变 image ID。tag + digest 固定内容但不验证签名、SBOM 或漏洞状态。

### 2.4 自动防回退

新增 `scripts/verify_dependency_locks.py`，只使用 Python 标准库并验证：

1. lock 语法、唯一 package、精确版本和 SHA-256；
2. 当前 pyproject direct dependencies/extras 覆盖；
3. 两个 Python stage 使用批准 digest；
4. Docker/CI 必须走 hashed binary lock，禁止恢复开放 editable dependency resolution；
5. 所有 Compose 外部 image 必须与九项批准 tag/digest 完全一致。

新增 `tests/unit/test_fs022_reproducible_dependencies.py` 对上述契约进行六类对抗验证；
FS-021 测试同步改为要求 CI lock 安装入口。

## 3. 验证结果

| 检查 | 结果 | 可信边界 |
|---|---|---|
| runtime target wheel + hash 下载 | `46 downloaded` | Windows pip cross-target，`--no-deps` 逐条验证 Linux CPython 3.12 wheel/hash |
| CI target wheel + hash 下载 | `33 downloaded` | 同上；闭包完整性由目标平台正常 hash install 最终失败关闭 |
| dependency verifier | `runtime=46 ci=33 external_images=9` | 标准库静态契约 |
| FS-021/022 focused | `13 passed` | 本机 Python 3.14；无外部服务 |
| focused Ruff | `All checks passed!` | verifier 与两份 contract tests |
| FS-010 generator contract | `8 passed` | 用唯一 basetemp；证明生成参考与现行文档逐字一致 |
| process lifecycle/Docker static | `22 passed` | 用唯一 basetemp；NATS lock、digest runtime stage、curl/tini 契约 |
| unit strict marker collection | `4411 tests collected` | `--strict-markers`；无 unknown unit marker |
| 标准完整 unit | `4381 passed, 30 skipped, 1659 warnings, 85 subtests passed in 108.44s` | 仓库内唯一 basetemp；无断言失败 |
| 全仓 Ruff | `All checks passed!` | `.venv` 中 Ruff 0.15.8 |
| Python environment consistency | `No broken requirements found` | 本机开发 venv 的 `pip check`，不是目标 lock install |
| workflow/Compose YAML | `YAML OK: 8 files` | PyYAML 静态解析；不是 Docker Compose semantic/build 验证 |
| 文档本地链接 | `785 files / 1230 local targets OK` | 忽略代码块/行号后检查目标存在；不验证外部 URL/anchor |
| diff whitespace | `git diff --check` 通过 | 行尾转换提示不是 whitespace error |

仓库要求的原样完整 unit 命令先运行，在 87 项通过后因 Windows 用户 temp 根目录 ACL 于
`tmp_path` setup 报 `PermissionError`，没有业务断言失败；随后使用仓库内本次唯一
`--basetemp` 重跑同一完整范围并得到上表结果。

当前主机没有 Docker CLI，因此没有执行 clean image build；WSL2 已知系统 Python 只有
3.10，不能替代目标 Python 3.12 构建。不得把 wheel 下载校验写成镜像已经可运行。

## 4. 关闭标准与残余风险

当前裁定：
`PARTIALLY REMEDIATED / PYTHON LOCKS & IMAGE DIGESTS ADDED / APT-SBOM-CVE & CLEAN BUILD OPEN`。

已收口的是 Python 发布/CI 依赖的版本与制品 hash、Python base/Compose 外部 image 内容
引用以及仓库内防回退路径。以下仍开放：

- runtime stage 的 Debian APT package 与 repository snapshot 未固定；
- clean Linux/Python 3.12 locked install 和 Docker multi-stage build 未执行；
- GitHub 远端 CI、required check 和 dependency update governance 未验证；
- SBOM、CVE、license、secret、malware、signature/attestation/provenance gate 未建立；
- registry/PyPI 可用性、撤包和 key compromise 的恢复流程未演练；
- 锁中版本的应用级 integration/Compose/schema/browser 回归未完成；
- 没有独立 reviewer 的 dependency/digest delta 复核。

因此 `FS-022` 不能标为 CLOSED，所有真实资金上线 gate 继续 NO-GO。本记录不授权部署、
实盘验证、远端设置修改或依赖自动升级。
