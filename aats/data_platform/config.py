"""Research Data Platform configuration.

All sensitive or environment-specific values are loaded from ``.env.research``
(project root).  Code defaults are intentionally set to non-functional
placeholders so the system will not accidentally connect with hardcoded
credentials.

The ``.env.research`` file is covered by ``.gitignore`` (matches ``.env.*``).
A template with documentation is provided at
``configs/templates/.env.research.example``.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env.research relative to the project root
# (config.py lives at aats/data_platform/config.py → project root is 3 levels up)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env.research"
_DEFAULT_RESEARCH_DATABASE_URL = "postgresql+psycopg://localhost:5432/aats_research"


class ResearchPlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RDP_",
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default=_DEFAULT_RESEARCH_DATABASE_URL,
        description="PostgreSQL DSN for the research database. "
                    "Set via RDP_DATABASE_URL in .env.research.",
    )

    # Historical file directories (daemon mode)
    historical_download_dir: str = Field(
        default="./data/historical",
        description="Root directory for OKX historical download files (legacy)",
    )
    historical_incoming_dir: str = Field(
        default="./data/historical/incoming",
        description="Incoming directory — place ZIP files here for auto-consumption",
    )
    historical_completed_dir: str = Field(
        default="./data/historical/completed",
        description="Completed directory — successfully consumed files are moved here",
    )
    historical_failed_dir: str = Field(
        default="./data/historical/failed",
        description="Failed directory — files that failed processing are moved here",
    )
    historical_scan_interval_seconds: int = Field(
        default=30,
        description="How often the historical daemon scans for new ZIP files",
    )

    # OKX API
    okx_rest_url: str = "https://www.okx.com"
    okx_timeout_seconds: float = 15.0
    okx_rate_limit_sleep: float = 0.12  # ~8 req/s conservative

    # Candles rolling
    rolling_candles_enabled: bool = True
    rolling_candles_symbols: list[str] = Field(
        default=["BTC-USDT", "ETH-USDT", "BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    )
    # NOTE (2026-04-07): 默认值仅保留 15m 和 1h, 因为:
    #   1. configs/active_parameter_sets/ 全部是 15m/1h, 没有 1m/5m 参数集
    #   2. decision_system/evidence_bundle 只评估 15m 与 1h family/timeframe
    #   3. 1m 在 daily 拉取下每天 1440 bar/symbol, 占 95% API 流量但无人消费
    # 如需重新启用 1m/5m, 设置 RDP_ROLLING_CANDLES_TIMEFRAMES 环境变量,
    # 或在 .env.research 中显式覆盖。schema 表 (staging/silver/gold *_1m, *_5m)
    # 仍然存在, 历史数据可继续读取, 只是默认不再增量采集。
    rolling_candles_timeframes: list[str] = Field(default=["15m", "1h"])

    # Funding rolling
    rolling_funding_enabled: bool = True
    rolling_funding_symbols: list[str] = Field(
        default=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    )

    # Gap repair
    auto_gap_repair_enabled: bool = True
    max_gap_repair_window_hours: int = 72

    # Gold build
    gold_replay_build_enabled: bool = True
    gold_auto_build_interval_cycles: int = Field(
        default=60,
        description="In realtime daemon: rebuild Gold every N rolling cycles",
    )

    # Gap repair
    gap_auto_detect_interval_cycles: int = Field(
        default=120,
        description="In realtime daemon: run gap detection every N rolling cycles",
    )
    gap_auto_detect_window_hours: int = Field(
        default=24,
        description="Gap detection lookback window in hours",
    )

    # Dataset version prefix
    dataset_version_prefix: str = "v1"

    # ── Live production DB (只读) ──────────────────────────────────────
    live_database_url: str | None = Field(
        default=None,
        description="Production DB readonly DSN. "
                    "Set via RDP_LIVE_DATABASE_URL in .env.research.",
    )
    live_db_readonly: bool = Field(
        default=True,
        description="Safety flag: enforce read-only access to live DB. "
                    "Must be True in production.",
    )
    live_db_schema: str | None = Field(
        default=None,
        description="Production DB schema name (default: public). "
                    "Set via RDP_LIVE_DB_SCHEMA if needed.",
    )
    live_db_connect_timeout_seconds: int = Field(
        default=10,
        description="Connection timeout for live DB in seconds.",
    )

    # ── 项目根目录 ─────────────────────────────────────────────────────
    project_root: str = Field(
        default=".",
        description="Project root directory for resolving artifact paths.",
    )

    # ── Active parameter set 目录 ──────────────────────────────────────
    active_parameter_sets_dir: str = Field(
        default="configs/active_parameter_sets",
        description="Directory for active parameter set files.",
    )

    def model_post_init(self, __context: object) -> None:
        """统一 research/governance DB 的默认解析链路.

        历史上 RDP 代码里同时存在两条治理库连接来源：
          1. AATS_ACTIVE_PARAMETER_DB_URL（gateway/compose 中实际注入）
          2. RDP_DATABASE_URL（.env.research / 本地脚本）

        当容器里没有显式设置 RDP_DATABASE_URL 时，BaseSettings 会退回到
        database_url 的默认值 ``localhost:5432/aats_research``，导致同一套
        RDP 页面里一部分查询连 @postgres，一部分健康检查却打到 localhost。

        这里约定：
          - 显式设置了 RDP_DATABASE_URL（或直接传入 database_url）时，以显式值为准
          - 否则，优先复用 AATS_ACTIVE_PARAMETER_DB_URL
          - 再否则，才回退到本地开发默认值
        """
        if "database_url" not in self.__pydantic_fields_set__:
            governance_url = str(os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL", "")).strip()
            if governance_url:
                object.__setattr__(self, "database_url", governance_url)


def get_settings() -> ResearchPlatformSettings:
    return ResearchPlatformSettings()
