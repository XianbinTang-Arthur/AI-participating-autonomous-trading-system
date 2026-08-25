# AATS 配置文档索引

> 文档状态：现行索引  
> 最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；Phase 3A–3W 整改提交候选）

配置文档入口如下：

- [`managed-config-reference.md`](managed-config-reference.md)：四个 managed profile、允许覆盖字段和配置优先级；
- [`../../configs/README.md`](../../configs/README.md)：模板、策略 YAML 与本地 override 的文件职责；
- [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)：部署时 profile、env-file、端口与 TLS 的操作入口。

固定配置行为以 `aats/bootstrap/config.py`、`aats/bootstrap/managed_profiles.py`、`aats/bootstrap/active_parameters.py`、`scripts/generate_managed_config_artifacts.py`、`configs/templates/` 和 `configs/strategy_profiles/` 为真源。managed strategy YAML 必须是 mapping 且零未知 `AATSSettings` key；生成器拥有 `.env.*.example` 与 managed reference，不覆盖人工治理的 `configs/README.md`。

运行时 active parameter 是 Postgres DB-only；静态 JSON 不是 fallback。任何 `.env.*` 都按凭证/私有配置处理，不在文档中读取或展示其内容。
