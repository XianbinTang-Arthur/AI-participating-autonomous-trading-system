# AATS Python 依赖锁定

> 文档状态：现行构建输入说明  
> 最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；未提交 Phase 3A–3V 工作区）  
> 适用平台：CPython 3.12、Linux x86_64、manylinux 2.17 或更新兼容宿主

本目录把人审可维护的顶层输入与机器生成的完整依赖闭包分开：

| 输入 | 生成文件 | 消费者 |
|---|---|---|
| `runtime-py312-linux-x86_64.in` | `runtime-py312-linux-x86_64.lock` | AATS Docker builder |
| `ci-py312-linux-x86_64.in` | `ci-py312-linux-x86_64.lock` | GitHub Actions Python quality job |

每个 lock 中所有第三方 distribution 都使用精确版本和 SHA-256 hash。Docker/CI 安装
必须同时使用 `--require-hashes` 与 `--only-binary=:all:`；本地 AATS 源码不属于 PyPI
distribution，Docker 在依赖安装后以 `--no-deps --no-build-isolation -e .` 安装，因此
不会重新解析第三方依赖。

## 受控更新

只在独立、无凭证的 CPython 3.12/Linux x86_64 环境中使用固定 `uv==0.12.5` 生成：

```bash
uv pip compile requirements/runtime-py312-linux-x86_64.in \
  --python-version 3.12 \
  --python-platform x86_64-manylinux_2_17 \
  --generate-hashes \
  --no-annotate \
  --no-emit-package aats \
  --output-file requirements/runtime-py312-linux-x86_64.lock

uv pip compile requirements/ci-py312-linux-x86_64.in \
  --python-version 3.12 \
  --python-platform x86_64-manylinux_2_17 \
  --generate-hashes \
  --no-annotate \
  --no-emit-package aats \
  --output-file requirements/ci-py312-linux-x86_64.lock
```

更新 PR 必须同时：

1. 审阅 direct/transitive 版本差异与 release/security 信息；
2. 在尚未安装第三方包的 Python 3.12 环境运行 `python scripts/verify_dependency_locks.py`；
3. 用 `pip download --no-deps --require-hashes --only-binary=:all:` 逐条验证 lock 中所有
   目标 archive；再在干净目标 venv 用不带 `--no-deps` 的正常 hash install 验证闭包；
4. 构建隔离镜像、生成 SBOM、执行 CVE/license/secret/provenance scan 与完整测试；
5. 记录基础镜像/Compose manifest digest 的来源、目标架构和复核时间。

## 可信边界

当前锁只覆盖 Python 3.12 Linux x86_64 的 Python distributions；Windows 本地开发可继续
使用 `pyproject.toml`，但不得把其解析结果当作发布镜像依赖真源。Debian apt 包、SBOM、
CVE/license 结果和远端 registry 可用性仍是 FS-022 的开放门禁。digest 固定保证引用
内容不随 tag 漂移，不证明镜像无漏洞或适合上线。
