# configs 目录职责

## 当前推荐路径

- `spot / derivatives / spot_live / derivatives_live` 四个托管 profile：
  - 运行时语义来自代码里的 managed profile 基线
  - 最小 override 来自项目根目录四个 `.env.*` 文件
  - 策略调参来自 `configs/strategy_profiles/*.yaml`

## legacy `configs/*.yaml` 的职责

- 只保留给非托管/manual `config_profile` 路径与测试使用
- 不再作为四个托管 profile 的主配置来源
- `base.yaml` 主要是本地演示/开发默认值说明，不是当前实盘推荐配置

## 目录说明

- `strategy_profiles/`：托管 profile 使用的策略调参文件
- `templates/`：自动生成的最小 `.env` 示例模板
- 其余 YAML：legacy/manual `config_profile` 路径或测试兼容

## 维护规则

- 账户、数据库、端口、日志、凭证类 override 改根目录 `.env.*`
- AI、自动换档、directional / smart_arbitrage / spot_grid / dca 调参改 `strategy_profiles/*.yaml`
- 若新增设置字段，优先更新 `aats/bootstrap/settings.py`，再决定它应归属 `.env` 还是 `strategy_profiles/*.yaml`

## unknown write 复核阈值放哪

- `AATS_EXECUTION_UNKNOWN_SUBMIT_REVIEW_AFTER_SECONDS`
- `AATS_EXECUTION_UNKNOWN_CANCEL_REVIEW_AFTER_SECONDS`
- 这两个字段属于执行恢复参数，应该放根目录对应 profile 的 `.env.*`，不要写进 `strategy_profiles/*.yaml`
