"""RDP (Research Data Platform) SQLAlchemy ORM 模型。

替代 migrations/research/*.sql，通过 RdpBase.metadata.create_all() 自动建表。
48 张表分布在 7 个 PostgreSQL schema：meta / staging / bronze / silver /
gold / research / governance。

设计决策：
- 静态 ORM class 用于结构独特的表（meta / research / governance，12 张）
- 工厂函数用于重复模式的数据层表（staging/bronze/silver candles + funding + gold replay bars，35 张）
- 所有模型仅用于 schema 定义（create_all），业务代码仍用原始 SQL 访问
- 省略 updated_at 触发器——开发阶段不需要，应用代码直接设值
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase

# ─────────────────────────────────────────────────────────────────────
# Declarative Base
# ─────────────────────────────────────────────────────────────────────

_RDP_SCHEMAS = ("meta", "staging", "bronze", "silver", "gold", "research", "governance")


class RdpBase(DeclarativeBase):
    """Research Data Platform 的 declarative base，独立于主交易库 Base。"""


# =====================================================================
# META Schema — 6 张表
# =====================================================================

class DatasetManifestModel(RdpBase):
    __tablename__ = "dataset_manifests"
    __table_args__ = (
        Index("idx_dm_layer_domain", "dataset_layer", "dataset_domain", "instrument_type", "timeframe"),
        Index("idx_dm_version", "dataset_version"),
        Index("idx_dm_status", "status"),
        CheckConstraint("dataset_layer IN ('staging','bronze','silver','gold')", name="chk_dm_layer"),
        CheckConstraint("dataset_domain IN ('candles','funding')", name="chk_dm_domain"),
        CheckConstraint("instrument_type IN ('spot','swap')", name="chk_dm_inst"),
        CheckConstraint("source_type IN ('historical_file','api','derived')", name="chk_dm_source"),
        CheckConstraint("status IN ('active','superseded','building','failed')", name="chk_dm_status"),
        {"schema": "meta"},
    )

    dataset_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    dataset_name = Column(Text, nullable=False)
    dataset_layer = Column(Text, nullable=False)
    dataset_domain = Column(Text, nullable=False)
    instrument_type = Column(Text, nullable=False)
    timeframe = Column(Text)
    symbol_scope = Column(Text, nullable=False)
    dataset_version = Column(Text, nullable=False)
    schema_version = Column(Text, nullable=False, server_default=text("'v1'"))
    source_type = Column(Text, nullable=False)
    source_dataset_ids = Column(ARRAY(UUID(as_uuid=False)), nullable=False, server_default=text("'{}'::uuid[]"))
    start_ts = Column(DateTime(timezone=True))
    end_ts = Column(DateTime(timezone=True))
    row_count = Column(BigInteger)
    status = Column(Text, nullable=False, server_default=text("'building'"))
    storage_table = Column(Text, nullable=False)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RawSourceFileModel(RdpBase):
    __tablename__ = "raw_source_files"
    __table_args__ = (
        Index("idx_rsf_domain", "dataset_domain", "instrument_type", "timeframe_hint"),
        Index("idx_rsf_checksum", "checksum"),
        Index("idx_rsf_status", "parse_status", "ingested_status"),
        UniqueConstraint("source_path", name="uq_rsf_path"),
        CheckConstraint("dataset_domain IN ('candles','funding')", name="chk_rsf_domain"),
        CheckConstraint("source_type IN ('historical_file','api_snapshot')", name="chk_rsf_source"),
        CheckConstraint("parse_status IN ('pending','parsed','failed')", name="chk_rsf_parse"),
        CheckConstraint("ingested_status IN ('pending','ingested','failed','skipped')", name="chk_rsf_ingest"),
        {"schema": "meta"},
    )

    source_file_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    source_type = Column(Text, nullable=False)
    dataset_domain = Column(Text, nullable=False)
    instrument_type = Column(Text)
    symbol_hint = Column(Text)
    timeframe_hint = Column(Text)
    source_granularity = Column(Text)
    source_path = Column(Text, nullable=False)
    checksum = Column(Text)
    file_size_bytes = Column(BigInteger)
    downloaded_at = Column(DateTime(timezone=True))
    discovered_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    source_start_ts = Column(DateTime(timezone=True))
    source_end_ts = Column(DateTime(timezone=True))
    raw_row_count = Column(BigInteger)
    parse_status = Column(Text, nullable=False, server_default=text("'pending'"))
    parse_error = Column(Text)
    ingested_status = Column(Text, nullable=False, server_default=text("'pending'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class IngestRunModel(RdpBase):
    __tablename__ = "ingest_runs"
    __table_args__ = (
        Index("idx_ir_type_status", "run_type", "dataset_domain", "status"),
        Index("idx_ir_symbol", "symbol", "timeframe"),
        Index("idx_ir_started", "started_at"),
        CheckConstraint("run_type IN ('backfill','rolling','gap_repair','gold_build')", name="chk_ir_type"),
        CheckConstraint("dataset_domain IN ('candles','funding')", name="chk_ir_domain"),
        CheckConstraint("status IN ('pending','running','succeeded','failed','retrying','backfilling')", name="chk_ir_status"),
        CheckConstraint("trigger_mode IN ('scheduler','manual','auto_gap_repair')", name="chk_ir_trigger"),
        {"schema": "meta"},
    )

    ingest_run_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    run_type = Column(Text, nullable=False)
    dataset_domain = Column(Text, nullable=False)
    instrument_type = Column(Text)
    symbol = Column(Text)
    timeframe = Column(Text)
    trigger_mode = Column(Text, nullable=False, server_default=text("'manual'"))
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    attempt_count = Column(Integer, nullable=False, server_default=text("1"))
    checkpoint_before = Column(JSONB)
    checkpoint_after = Column(JSONB)
    error_message = Column(Text)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class IngestRunItemModel(RdpBase):
    __tablename__ = "ingest_run_items"
    __table_args__ = (
        Index("idx_iri_run", "ingest_run_id"),
        Index("idx_iri_status", "dataset_domain", "symbol", "timeframe", "status"),
        CheckConstraint("dataset_domain IN ('candles','funding')", name="chk_iri_domain"),
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="chk_iri_status"),
        {"schema": "meta"},
    )

    ingest_run_item_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    ingest_run_id = Column(UUID(as_uuid=False), ForeignKey("meta.ingest_runs.ingest_run_id"), nullable=False)
    dataset_domain = Column(Text, nullable=False)
    instrument_type = Column(Text)
    symbol = Column(Text)
    timeframe = Column(Text)
    window_start_ts = Column(DateTime(timezone=True))
    window_end_ts = Column(DateTime(timezone=True))
    source_file_id = Column(UUID(as_uuid=False), ForeignKey("meta.raw_source_files.source_file_id"))
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    raw_rows_read = Column(BigInteger)
    rows_written_staging = Column(BigInteger)
    rows_written_bronze = Column(BigInteger)
    rows_written_silver = Column(BigInteger)
    rows_written_gold = Column(BigInteger)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class IngestCheckpointModel(RdpBase):
    __tablename__ = "ingest_checkpoints"
    __table_args__ = (
        UniqueConstraint("dataset_domain", "instrument_type", "symbol", "timeframe", name="uq_checkpoint_key"),
        Index("idx_cp_status", "checkpoint_status"),
        Index("idx_cp_symbol", "symbol", "timeframe"),
        CheckConstraint("dataset_domain IN ('candles','funding')", name="chk_cp_domain"),
        CheckConstraint("instrument_type IN ('spot','swap')", name="chk_cp_inst"),
        CheckConstraint("checkpoint_status IN ('active','stale','gap_detected')", name="chk_cp_status"),
        {"schema": "meta"},
    )

    checkpoint_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    dataset_domain = Column(Text, nullable=False)
    instrument_type = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    timeframe = Column(Text)
    last_successful_ts = Column(DateTime(timezone=True))
    last_attempted_ts = Column(DateTime(timezone=True))
    next_expected_ts = Column(DateTime(timezone=True))
    backfill_completed = Column(Boolean, nullable=False, server_default=text("false"))
    gap_detected = Column(Boolean, nullable=False, server_default=text("false"))
    gap_start_ts = Column(DateTime(timezone=True))
    gap_end_ts = Column(DateTime(timezone=True))
    checkpoint_status = Column(Text, nullable=False, server_default=text("'active'"))
    last_ingest_run_id = Column(UUID(as_uuid=False))
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class QualityReportModel(RdpBase):
    __tablename__ = "quality_reports"
    __table_args__ = (
        Index("idx_qr_layer", "dataset_layer", "dataset_domain", "instrument_type", "timeframe"),
        Index("idx_qr_status", "quality_status"),
        Index("idx_qr_version", "dataset_version"),
        Index("idx_qr_run", "ingest_run_id"),
        CheckConstraint("dataset_layer IN ('staging','bronze','silver','gold')", name="chk_qr_layer"),
        CheckConstraint("dataset_domain IN ('candles','funding')", name="chk_qr_domain"),
        CheckConstraint("quality_status IN ('pass','warn','fail')", name="chk_qr_status"),
        {"schema": "meta"},
    )

    quality_report_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    ingest_run_id = Column(UUID(as_uuid=False), ForeignKey("meta.ingest_runs.ingest_run_id"))
    dataset_layer = Column(Text, nullable=False)
    dataset_domain = Column(Text, nullable=False)
    instrument_type = Column(Text)
    symbol = Column(Text)
    timeframe = Column(Text)
    dataset_version = Column(Text, nullable=False)
    window_start_ts = Column(DateTime(timezone=True))
    window_end_ts = Column(DateTime(timezone=True))
    total_rows = Column(BigInteger, nullable=False, server_default=text("0"))
    missing_intervals_count = Column(Integer, nullable=False, server_default=text("0"))
    duplicate_rows_count = Column(Integer, nullable=False, server_default=text("0"))
    out_of_order_rows_count = Column(Integer, nullable=False, server_default=text("0"))
    invalid_price_rows_count = Column(Integer, nullable=False, server_default=text("0"))
    invalid_volume_rows_count = Column(Integer, nullable=False, server_default=text("0"))
    suspect_rows_count = Column(Integer, nullable=False, server_default=text("0"))
    quality_status = Column(Text, nullable=False, server_default=text("'pass'"))
    details = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# =====================================================================
# DATA LAYER — 工厂生成的 candle / funding / replay_bar 表
# =====================================================================
# staging: 8 candle + 1 funding = 9（PK = staging_row_id BIGSERIAL）
# bronze:  8 candle + 1 funding = 9（PK = (symbol, ts)）
# silver:  8 candle + 1 funding = 9（PK = (symbol, ts)）
# gold:    8 replay_bar          = 8（PK = (symbol, ts)）
# 共 35 张表
# =====================================================================

_LAYER_PREFIX = {"staging": "stg", "bronze": "brz", "silver": "slv"}
_INST_TYPES = ("spot", "swap")
_TIMEFRAMES = ("1m", "5m", "15m", "1h")

# 保存工厂生成的 class 引用，防止 GC 并可供外部检视
_data_layer_models: dict[str, type] = {}


def _make_candle_model(layer: str, inst_type: str, tf: str) -> type:
    """为 staging/bronze/silver 的一张 candle 表生成 ORM class。"""
    tbl = f"market_{inst_type}_candles_{tf}"
    is_staging = layer == "staging"
    pfx = _LAYER_PREFIX[layer]

    # ── indexes ──
    indexes: list[Index] = []
    if is_staging:
        indexes.append(Index(f"idx_{pfx}_{inst_type}_candles_{tf}_sym_ts", "symbol", "ts"))
        indexes.append(Index(f"idx_{pfx}_{inst_type}_candles_{tf}_run", "ingest_run_id"))
    else:
        indexes.append(Index(f"idx_{pfx}_{inst_type}_candles_{tf}_ts", "ts"))
        indexes.append(Index(f"idx_{pfx}_{inst_type}_candles_{tf}_run", "ingest_run_id"))
        if layer == "bronze":
            indexes.append(Index(f"idx_{pfx}_{inst_type}_candles_{tf}_sf", "source_file_id"))
        if layer == "silver":
            indexes.append(Index(f"idx_{pfx}_{inst_type}_candles_{tf}_ver", "dataset_version"))

    # ── columns ──
    attrs: dict[str, object] = {
        "__tablename__": tbl,
        "__table_args__": (*indexes, {"schema": layer}),
    }

    if is_staging:
        attrs["staging_row_id"] = Column(BigInteger, primary_key=True, autoincrement=True)
        attrs["symbol"] = Column(Text, nullable=False)
        attrs["ts"] = Column(DateTime(timezone=True), nullable=False)
    else:
        attrs["symbol"] = Column(Text, primary_key=True)
        attrs["ts"] = Column(DateTime(timezone=True), primary_key=True)

    attrs.update({
        "open": Column(Numeric(20, 10), nullable=False),
        "high": Column(Numeric(20, 10), nullable=False),
        "low": Column(Numeric(20, 10), nullable=False),
        "close": Column(Numeric(20, 10), nullable=False),
        "vol": Column(Numeric(28, 10)),
        "vol_ccy": Column(Numeric(28, 10)),
        "vol_quote": Column(Numeric(28, 10)),
        "confirm": Column(Boolean, nullable=False, server_default=text("true")),
        "raw_symbol": Column(Text),
        "raw_ts": Column(Text),
        "source_file_id": Column(UUID(as_uuid=False)),
        "ingest_run_id": Column(UUID(as_uuid=False), nullable=False),
        "dataset_version": Column(Text, nullable=False),
        "quality_flags": Column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
        "created_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
        "updated_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    })

    cls_name = f"_{layer.title()}{inst_type.title()}Candle{tf}Model"
    model = type(cls_name, (RdpBase,), attrs)
    _data_layer_models[f"{layer}.{tbl}"] = model
    return model


def _make_funding_model(layer: str) -> type:
    """为 staging/bronze/silver 的 funding 表生成 ORM class。"""
    tbl = "market_swap_funding"
    is_staging = layer == "staging"
    pfx = _LAYER_PREFIX[layer]

    indexes: list[Index] = []
    if is_staging:
        indexes.append(Index(f"idx_{pfx}_swap_funding_sym_ts", "symbol", "ts"))
        indexes.append(Index(f"idx_{pfx}_swap_funding_run", "ingest_run_id"))
    else:
        indexes.append(Index(f"idx_{pfx}_swap_funding_ts", "ts"))
        indexes.append(Index(f"idx_{pfx}_swap_funding_run", "ingest_run_id"))
        if layer == "silver":
            indexes.append(Index(f"idx_{pfx}_swap_funding_ver", "dataset_version"))

    attrs: dict[str, object] = {
        "__tablename__": tbl,
        "__table_args__": (*indexes, {"schema": layer}),
    }

    if is_staging:
        attrs["staging_row_id"] = Column(BigInteger, primary_key=True, autoincrement=True)
        attrs["symbol"] = Column(Text, nullable=False)
        attrs["ts"] = Column(DateTime(timezone=True), nullable=False)
    else:
        attrs["symbol"] = Column(Text, primary_key=True)
        attrs["ts"] = Column(DateTime(timezone=True), primary_key=True)

    attrs.update({
        "funding_rate": Column(Numeric(18, 12), nullable=False),
        "inst_type": Column(Text),
        "formula_type": Column(Text),
        "method": Column(Text),
        "realized_rate": Column(Numeric(18, 12)),
        "raw_symbol": Column(Text),
        "raw_ts": Column(Text),
        "source_file_id": Column(UUID(as_uuid=False)),
        "ingest_run_id": Column(UUID(as_uuid=False), nullable=False),
        "dataset_version": Column(Text, nullable=False),
        "quality_flags": Column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
        "created_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
        "updated_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    })

    cls_name = f"_{layer.title()}SwapFundingModel"
    model = type(cls_name, (RdpBase,), attrs)
    _data_layer_models[f"{layer}.{tbl}"] = model
    return model


def _make_replay_bar_model(inst_type: str, tf: str) -> type:
    """为 gold 的一张 replay_bar 表生成 ORM class。"""
    tbl = f"market_{inst_type}_replay_bars_{tf}"

    indexes = [
        Index(f"idx_gld_{inst_type}_replay_bars_{tf}_ts", "ts"),
        Index(f"idx_gld_{inst_type}_replay_bars_{tf}_bld", "build_run_id"),
        Index(f"idx_gld_{inst_type}_replay_bars_{tf}_ver", "source_candle_dataset_version"),
    ]

    attrs: dict[str, object] = {
        "__tablename__": tbl,
        "__table_args__": (*indexes, {"schema": "gold"}),
        "symbol": Column(Text, primary_key=True),
        "ts": Column(DateTime(timezone=True), primary_key=True),
        "open": Column(Numeric(20, 10), nullable=False),
        "high": Column(Numeric(20, 10), nullable=False),
        "low": Column(Numeric(20, 10), nullable=False),
        "close": Column(Numeric(20, 10), nullable=False),
        "volume": Column(Numeric(28, 10)),
        "quote_volume": Column(Numeric(28, 10)),
        "is_closed": Column(Boolean, nullable=False, server_default=text("true")),
        "aligned_funding_rate": Column(Numeric(18, 12)),
        "funding_source_ts": Column(DateTime(timezone=True)),
        "source_candle_dataset_version": Column(Text, nullable=False),
        "source_funding_dataset_version": Column(Text),
        "build_run_id": Column(UUID(as_uuid=False), nullable=False),
        "quality_flags": Column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
        "created_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
        "updated_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    }

    cls_name = f"_Gold{inst_type.title()}ReplayBar{tf}Model"
    model = type(cls_name, (RdpBase,), attrs)
    _data_layer_models[f"gold.{tbl}"] = model
    return model


# ── 注册全部 35 张数据层表 ──
for _layer in ("staging", "bronze", "silver"):
    for _inst in _INST_TYPES:
        for _tf in _TIMEFRAMES:
            _make_candle_model(_layer, _inst, _tf)
    _make_funding_model(_layer)

for _inst in _INST_TYPES:
    for _tf in _TIMEFRAMES:
        _make_replay_bar_model(_inst, _tf)


# =====================================================================
# STAGING — 附加静态表（非 candle / funding 模式）
# =====================================================================

class RawLiquidationsModel(RdpBase):
    """staging.raw_liquidations — OKX liquidation-orders WebSocket 原始流落库。

    承载 OKX public `liquidation-orders` 频道推送的每条 details 行。OKX REST
    `/api/v5/public/liquidation-orders` 仅保留 7 天历史，本表是 data lake 侧
    长期积累的唯一来源，供未来 baseline contrarian 反转信号回填使用。

    Natural key 是 (inst_id, ts, side, bk_px, sz) —— OKX 重连 / 广播重发时
    会看到相同事件，靠 UNIQUE 约束 + ON CONFLICT DO NOTHING 做 DB 级幂等。
    """
    __tablename__ = "raw_liquidations"
    __table_args__ = (
        UniqueConstraint(
            "inst_id", "ts", "side", "bk_px", "sz",
            name="uq_raw_liquidations_natural_key",
        ),
        Index("ix_raw_liquidations_inst_ts", "inst_id", "ts"),
        Index("ix_raw_liquidations_received", "received_at"),
        CheckConstraint("side IN ('buy','sell')", name="chk_raw_liq_side"),
        {"schema": "staging"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    inst_id = Column(Text, nullable=False)
    inst_type = Column(Text, nullable=False)
    inst_family = Column(Text)
    side = Column(Text, nullable=False)
    bk_px = Column(Numeric(28, 10), nullable=False)
    sz = Column(Numeric(28, 10), nullable=False)
    bk_loss = Column(Numeric(28, 10))
    ccy = Column(Text)
    raw_payload = Column(JSONB, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# =====================================================================
# BRONZE / STAGING — P1-D Phase 1A microstructure 表 (§6)
# =====================================================================
# 4 张表供 OKX `trades-all` / `bbo-tbt` / `books5` / open-interest-funding-mark
# 三大 WS 频道落库。参考 docs/design/p1d_phase1a_implementation_design_2026_04_20.md。
# 实际 DDL 由 migrations/batch_b_05_microstructure.sql 承载,ORM 仅供 create_all
# 兜底 + 单元测试 + 程序化读写。

class BronzeMarketTradesModel(RdpBase):
    """bronze.market_trades — OKX trades-all WS 频道落库。

    OKX `tradeId` 是全局唯一递增整数;重连重发靠 (symbol, ts, trade_id) 复合
    主键 + INSERT ... ON CONFLICT DO NOTHING 做 DB 级幂等。同一 ts 可能有多笔
    trade(尤其 liquidation cascade 时高频),因此 trade_id 必须进入 PK。

    热路径 ETL 走 (symbol, ts) 窗口扫描,复合索引 idx_brz_market_trades_sym_ts
    支持;独立 trade_id 索引不加(PK 已含,symbol 是强过滤)。
    """
    __tablename__ = "market_trades"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", "trade_id", name="pk_brz_market_trades"),
        Index("idx_brz_market_trades_ts", "ts"),
        Index("idx_brz_market_trades_sym_ts", "symbol", "ts"),
        CheckConstraint("side IN ('buy','sell')", name="chk_brz_trades_side"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # OKX trade.ts (ms → utc)
    trade_id = Column(Text, nullable=False)                          # OKX tradeId, string
    px = Column(Numeric(20, 10), nullable=False)
    sz = Column(Numeric(28, 10), nullable=False)
    side = Column(Text, nullable=False)                              # 'buy' or 'sell' (taker side)
    raw_payload = Column(JSONB)                                      # 仅保留 OKX detail, 不含 arg
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class BronzeMarketOrderbookBboModel(RdpBase):
    """bronze.market_orderbook_bbo — OKX bbo-tbt WS 频道 1Hz 采样落库。

    OKX bbo-tbt 原推送 10ms,客户端限流采样 1Hz(1 行/秒/symbol),Phase 1A
    采样率对齐可行性报告 §4.1;Phase 2A regression 后评估是否提升到 10Hz。

    mid / spread / imbalance 三个 GENERATED ALWAYS AS ... STORED 列在 DB 层
    自动计算,避免 Silver ETL 每次重算。ORM 里用 SQLAlchemy Computed(...,
    persisted=True),对 PostgreSQL 与 SQLite 3.31+ 都生成兼容 DDL。
    """
    __tablename__ = "market_orderbook_bbo"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_brz_market_orderbook_bbo"),
        Index("idx_brz_market_orderbook_bbo_ts", "ts"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # 客户端采样时刻
    source_ts = Column(DateTime(timezone=True), nullable=False)      # OKX 推送原 ts
    bid_px = Column(Numeric(20, 10), nullable=False)
    bid_sz = Column(Numeric(28, 10), nullable=False)
    ask_px = Column(Numeric(20, 10), nullable=False)
    ask_sz = Column(Numeric(28, 10), nullable=False)
    # 便利性计算字段 (GENERATED ALWAYS AS ... STORED)
    mid = Column(
        Numeric(20, 10),
        Computed("(bid_px + ask_px) / 2", persisted=True),
    )
    spread = Column(
        Numeric(20, 10),
        Computed("ask_px - bid_px", persisted=True),
    )
    imbalance = Column(
        Numeric(18, 10),
        Computed(
            "CASE WHEN (bid_sz + ask_sz) > 0 "
            "THEN (bid_sz - ask_sz) / (bid_sz + ask_sz) "
            "ELSE 0 END",
            persisted=True,
        ),
    )
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class BronzeMarketOrderbookBooks5Model(RdpBase):
    """bronze.market_orderbook_books5 — OKX books5 WS 频道 2Hz 采样落库。

    OKX books5 原推送 100ms,客户端限流采样 2Hz (500ms 一行),5 档深度展平
    为 20 列 NUMERIC 避免 JSONB 解析开销。Level 1 (top) 为 NOT NULL,其余
    level 2-5 可 NULL (OKX 有时只返回 < 5 档)。
    """
    __tablename__ = "market_orderbook_books5"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_brz_market_orderbook_books5"),
        Index("idx_brz_market_orderbook_books5_ts", "ts"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # 客户端采样时刻
    source_ts = Column(DateTime(timezone=True), nullable=False)      # OKX 推送原 ts
    # Level 1 NOT NULL (top-of-book 总是存在)
    bid_px_1 = Column(Numeric(20, 10), nullable=False)
    bid_sz_1 = Column(Numeric(28, 10), nullable=False)
    # Level 2-5 可 NULL (OKX 有时不足 5 档)
    bid_px_2 = Column(Numeric(20, 10))
    bid_sz_2 = Column(Numeric(28, 10))
    bid_px_3 = Column(Numeric(20, 10))
    bid_sz_3 = Column(Numeric(28, 10))
    bid_px_4 = Column(Numeric(20, 10))
    bid_sz_4 = Column(Numeric(28, 10))
    bid_px_5 = Column(Numeric(20, 10))
    bid_sz_5 = Column(Numeric(28, 10))
    ask_px_1 = Column(Numeric(20, 10), nullable=False)
    ask_sz_1 = Column(Numeric(28, 10), nullable=False)
    ask_px_2 = Column(Numeric(20, 10))
    ask_sz_2 = Column(Numeric(28, 10))
    ask_px_3 = Column(Numeric(20, 10))
    ask_sz_3 = Column(Numeric(28, 10))
    ask_px_4 = Column(Numeric(20, 10))
    ask_sz_4 = Column(Numeric(28, 10))
    ask_px_5 = Column(Numeric(20, 10))
    ask_sz_5 = Column(Numeric(28, 10))
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class StagingMarketOiFundingTicksModel(RdpBase):
    """staging.market_oi_funding_ticks — open-interest / funding-rate /
    mark-price 三个 OKX WS 频道统一 tick 表。

    为什么放 staging 而非 bronze: 同 staging.raw_liquidations,这是每 tick
    原始流,Silver ETL 直接 group-by 聚合为 silver_*_15m bar,不需要独立的
    bronze 精简层。

    BIGSERIAL id PK: 同一 (symbol, ts, tick_type) 在 OKX 推送毫秒级并发时
    可能冲突,BIGSERIAL 避免 PK 冲突;ingest 是 append-only,不做 upsert。

    tick_type 列区分语义: 'oi' 时 oi/oi_ccy 有值,'funding' 时 funding_rate/
    next_funding_rate/next_funding_time 有值, 'mark' 时 mark_px 有值。
    """
    __tablename__ = "market_oi_funding_ticks"
    __table_args__ = (
        Index("ix_staging_market_oif_sym_ts", "symbol", "ts"),
        Index("ix_staging_market_oif_type_ts", "tick_type", "ts"),
        CheckConstraint(
            "tick_type IN ('oi','funding','mark')",
            name="chk_staging_oif_type",
        ),
        {"schema": "staging"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)             # OKX 推送 ts
    symbol = Column(Text, nullable=False)
    tick_type = Column(Text, nullable=False)                         # 'oi' | 'funding' | 'mark'
    oi = Column(Numeric(28, 10))                                     # when tick_type='oi'
    oi_ccy = Column(Numeric(28, 10))
    funding_rate = Column(Numeric(18, 12))                           # when tick_type='funding'
    next_funding_rate = Column(Numeric(18, 12))
    next_funding_time = Column(DateTime(timezone=True))
    mark_px = Column(Numeric(20, 10))                                # when tick_type='mark'
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


# =====================================================================
# RESEARCH Schema — 3 张表
# =====================================================================

class ParameterScanRunModel(RdpBase):
    __tablename__ = "parameter_scan_runs"
    __table_args__ = (
        Index("idx_psr_status", "status"),
        Index("idx_psr_family", "family", "symbol", "timeframe"),
        CheckConstraint("family IN ('independent','directional')", name="chk_psr_family"),
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="chk_psr_status"),
        {"schema": "research"},
    )

    scan_run_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    family = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    timeframe = Column(Text, nullable=False)
    dataset_version = Column(Text, nullable=False)
    parameter_grid = Column(JSONB, nullable=False)
    total_combinations = Column(Integer, nullable=False, server_default=text("0"))
    completed_count = Column(Integer, nullable=False, server_default=text("0"))
    failed_count = Column(Integer, nullable=False, server_default=text("0"))
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    comparison_path = Column(Text)
    notes = Column(Text)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ExperimentModel(RdpBase):
    __tablename__ = "experiments"
    __table_args__ = (
        Index("idx_exp_family", "family", "symbol", "timeframe"),
        Index("idx_exp_status", "status"),
        Index("idx_exp_version", "dataset_version"),
        CheckConstraint("family IN ('independent','directional')", name="chk_exp_family"),
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="chk_exp_status"),
        {"schema": "research"},
    )

    experiment_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    family = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    timeframe = Column(Text, nullable=False)
    dataset_version = Column(Text, nullable=False)
    parameter_overrides = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    window_start_ts = Column(DateTime(timezone=True))
    window_end_ts = Column(DateTime(timezone=True))
    bar_count = Column(Integer)
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    error_message = Column(Text)
    result_path = Column(Text)
    summary_path = Column(Text)
    report_path = Column(Text)
    scan_run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("research.parameter_scan_runs.scan_run_id", ondelete="SET NULL"),
    )
    notes = Column(Text)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ExperimentSummaryModel(RdpBase):
    __tablename__ = "experiment_summaries"
    __table_args__ = (
        UniqueConstraint("experiment_id", name="uq_expsum_exp"),
        {"schema": "research"},
    )

    experiment_summary_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    experiment_id = Column(
        UUID(as_uuid=False),
        ForeignKey("research.experiments.experiment_id"),
        nullable=False,
        unique=True,
    )
    total_bars = Column(Integer, nullable=False, server_default=text("0"))
    opening_count = Column(Integer, nullable=False, server_default=text("0"))
    blocked_count = Column(Integer, nullable=False, server_default=text("0"))
    hold_count = Column(Integer, nullable=False, server_default=text("0"))
    close_count = Column(Integer, nullable=False, server_default=text("0"))
    selectable_count = Column(Integer, nullable=False, server_default=text("0"))
    execution_compatible_count = Column(Integer, nullable=False, server_default=text("0"))
    selectable_ratio = Column(Double)
    execution_compatible_ratio = Column(Double)
    mean_long_score = Column(Double)
    mean_short_score = Column(Double)
    mean_expected_edge_bps = Column(Double)
    median_expected_edge_bps = Column(Double)
    p25_expected_edge_bps = Column(Double)
    p75_expected_edge_bps = Column(Double)
    top_blocking_reasons = Column(JSONB)
    state_distribution = Column(JSONB)
    action_distribution = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# =====================================================================
# GOVERNANCE Schema — 6 张表
# =====================================================================

class ActiveParameterSetModel(RdpBase):
    __tablename__ = "active_parameter_sets"
    __table_args__ = (
        UniqueConstraint("family", "timeframe", name="uq_active_combo"),
        ForeignKeyConstraint(
            ["parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_active_ps_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    family = Column(String(64), nullable=False)
    timeframe = Column(String(16), nullable=False)
    parameter_set_id = Column(String(128), nullable=False)
    values = Column(JSONB, nullable=False)
    source_round_id = Column(String(128))
    approval_recommendation_id = Column(String(128))
    applied_by = Column(String(128), nullable=False, server_default=text("'operator'"))
    applied_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ParameterApplyHistoryModel(RdpBase):
    __tablename__ = "parameter_apply_history"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_apply_op"),
        Index("ix_apply_history_combo", "family", "timeframe", "created_at"),
        ForeignKeyConstraint(
            ["to_parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_apply_history_to_ps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["from_parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_apply_history_from_ps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "operation_type IN ('apply', 'rollback', 'clear')",
            name="ck_apply_op_type",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    operation_id = Column(String(128), nullable=False, unique=True)
    operation_type = Column(String(32), nullable=False)
    family = Column(String(64), nullable=False)
    timeframe = Column(String(16), nullable=False)
    from_parameter_set_id = Column(String(128))
    to_parameter_set_id = Column(String(128))
    recommendation_id = Column(String(128))
    actor = Column(String(128), nullable=False, server_default=text("'operator'"))
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ParameterSetModel(RdpBase):
    """governance.parameter_sets — 参数集候选池（draft/candidate/frozen/deprecated）.

    对应文件: artifacts/governance/current_parameter_registry.json 中的 parameter_sets 列表。
    每条记录是一个版本化的参数集，经历 draft → candidate → frozen → deprecated 生命周期。
    """

    __tablename__ = "parameter_sets"
    __table_args__ = (
        UniqueConstraint("parameter_set_id", name="uq_ps_id"),
        Index("ix_ps_family_tf_status", "family", "timeframe", "status"),
        Index("ix_ps_source_round", "source_round_id"),
        # Bug 9 修复 (2026-04-19): 加入 'released' 状态 —— apply 事务把 target
        # parameter_set 从 candidate 升 released，保持 "每 combo 任一时刻最多 1 条
        # released" 的 invariant。之前 CHECK 只允许 {draft, candidate, frozen,
        # deprecated}，导致新 DB 上跑 apply 时 CHECK violation。
        # forward-compat: 未来 freeze API 恢复时 frozen 仍保留在 allowlist。
        CheckConstraint(
            "status IN ('draft', 'candidate', 'frozen', 'released', 'deprecated')",
            name="ck_ps_status",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    parameter_set_id = Column(String(128), nullable=False)
    family = Column(String(64), nullable=False)
    symbol = Column(String(32), nullable=False, server_default=text("'BTC-USDT-SWAP'"))
    timeframe = Column(String(16), nullable=False)
    source_round_id = Column(String(128))
    source_phase = Column(String(64))
    dataset_version = Column(String(32), nullable=False, server_default=text("'v1.0'"))
    values = Column(JSONB, nullable=False)
    confidence = Column(String(32))
    status = Column(String(32), nullable=False, server_default=text("'draft'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    frozen_at = Column(DateTime(timezone=True))
    deprecated_at = Column(DateTime(timezone=True))
    notes = Column(Text)


class RecommendationModel(RdpBase):
    """governance.recommendations — 参数变更审批建议.

    对应文件: artifacts/decision_system/recommendation_registry.json 中的 recommendations 列表。
    状态流: draft → approved / rejected / superseded。
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_rec_id"),
        Index("ix_rec_family_tf_status", "family", "timeframe", "status"),
        Index(
            "uq_rec_round_family_tf_active",
            "source_round_id", "family", "timeframe",
            unique=True,
            postgresql_where=text(
                "source_round_id IS NOT NULL "
                "AND status NOT IN ('superseded', 'rejected')"
            ),
        ),
        CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'superseded')",
            name="ck_rec_status",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = Column(String(128), nullable=False)
    family = Column(String(64), nullable=False)
    symbol = Column(String(32), nullable=False, server_default=text("'BTC-USDT-SWAP'"))
    timeframe = Column(String(16), nullable=False)
    recommendation_type = Column(String(32), nullable=False)
    target_parameter_set_id = Column(String(128))
    source_round_id = Column(String(128))
    confidence = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False)
    evidence_bundle_ref = Column(String(128))
    status = Column(String(32), nullable=False, server_default=text("'draft'"))
    approved_by = Column(String(128))
    approved_at = Column(DateTime(timezone=True))
    review_notes = Column(Text)
    rejected_by = Column(String(128))
    rejected_at = Column(DateTime(timezone=True))
    superseded_by = Column(String(128))
    superseded_at = Column(DateTime(timezone=True))
    superseded_by_recommendation_id = Column(String(128))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ActiveDecisionModel(RdpBase):
    """governance.active_decisions — 每个 family/timeframe 的当前决策状态.

    对应文件: artifacts/decision_system/active_decision_registry.json 中的 decisions 列表。
    每个 (family, timeframe) 只有一条记录（UPSERT 语义）。
    """

    __tablename__ = "active_decisions"
    __table_args__ = (
        UniqueConstraint("family", "timeframe", name="uq_active_decision_combo"),
        ForeignKeyConstraint(
            ["active_parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_active_decision_ps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    family = Column(String(64), nullable=False)
    symbol = Column(String(32), nullable=False, server_default=text("'BTC-USDT-SWAP'"))
    timeframe = Column(String(16), nullable=False)
    combo_key = Column(String(128), nullable=False)
    current_status = Column(String(64), nullable=False)
    active_parameter_set_id = Column(String(128))
    last_recommendation_id = Column(String(128))
    last_updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    notes = Column(Text)


class DecisionRoundSnapshotModel(RdpBase):
    """governance.decision_round_snapshots — Phase 6 最新 round 的 DB-first 快照."""

    __tablename__ = "decision_round_snapshots"
    __table_args__ = (
        UniqueConstraint("round_id", name="uq_decision_round_snapshot_round_id"),
        Index("ix_decision_round_snapshot_finished", "finished_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(String(128), nullable=False, unique=True)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    evidence_summary_json = Column(Text)
    parameter_upgrade_candidates_json = Column(Text)
    family_timeframe_decisions_json = Column(Text)
    promotion_readiness_json = Column(Text)
    manifest_json = Column(Text)
    conclusion_markdown = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class GovernanceSnapshotModel(RdpBase):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_type", name="uq_governance_snapshot_type"),
        Index("ix_governance_snapshot_generated", "generated_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_type = Column(String(64), nullable=False, unique=True)
    generated_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ResearchRoundSnapshotModel(RdpBase):
    __tablename__ = "research_round_snapshots"
    __table_args__ = (
        UniqueConstraint("round_id", name="uq_research_round_snapshot_round_id"),
        Index("ix_research_round_snapshot_phase_finished", "phase", "finished_at"),
        Index("ix_research_round_snapshot_phase_started", "phase", "started_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(String(128), nullable=False, unique=True)
    phase = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'unknown'"))
    round_path = Column(Text)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    replay_only = Column(Boolean, nullable=False, server_default=text("false"))
    manifest_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    summary_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    conclusion_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    artifacts_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RdpTaskQueueModel(RdpBase):
    __tablename__ = "rdp_task_queue"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_rdp_task_id"),
        Index("ix_rdp_task_queue_status", "status", "created_at"),
        Index(
            "ix_rdp_task_one_active_per_workflow",
            "workflow",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        CheckConstraint("status IN ('pending','running','done','failed')", name="chk_rdp_task_status"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(128), nullable=False, unique=True)
    workflow = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'pending'"))
    requested_by = Column(String(128), nullable=False, server_default=text("'operator'"))
    requested_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    # R3 Bug 6 retry 延迟机制 (2026-04-19):
    # daemon claim 时要求 earliest_start_at <= now()，让 auto_retry task 能延迟
    # 15min 后才被领取。scheduler 正常入队时默认 = created_at（立即可领）。
    # 现有数据 server_default='now()' 自动兼容（升级后旧 row 立刻 claimable）。
    earliest_start_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    exit_code = Column(Integer)
    error_message = Column(Text)
    log_tail = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RdpRuntimeStatusModel(RdpBase):
    """governance.rdp_runtime_status — 跨容器共享的运行态心跳."""

    __tablename__ = "rdp_runtime_status"
    __table_args__ = (
        UniqueConstraint("component", name="uq_rdp_runtime_status_component"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    component = Column(String(64), nullable=False, unique=True)
    status = Column(String(32), nullable=False, server_default=text("'unknown'"))
    heartbeat_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    details_json = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class WorkflowRunReportModel(RdpBase):
    __tablename__ = "workflow_run_reports"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_workflow_run_report_run_id"),
        Index("ix_workflow_run_report_workflow_finished", "workflow", "finished_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(128), nullable=False, unique=True)
    workflow = Column(String(64), nullable=False)
    overall_status = Column(String(32), nullable=False)
    description = Column(Text)
    report = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class WorkflowSchedulerStateModel(RdpBase):
    __tablename__ = "workflow_scheduler_state"
    __table_args__ = (
        UniqueConstraint("workflow", name="uq_workflow_scheduler_state_workflow"),
        Index("ix_workflow_scheduler_state_checked", "last_checked_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow = Column(String(64), nullable=False, unique=True)
    initialized_at = Column(DateTime(timezone=True))
    last_processed_slot = Column(DateTime(timezone=True))
    last_action = Column(String(64))
    last_checked_at = Column(DateTime(timezone=True))
    last_task_id = Column(String(128))
    last_reason = Column(Text)
    schedule = Column(String(128))
    state_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class PreApplyGateResultModel(RdpBase):
    __tablename__ = "pre_apply_gate_results"
    __table_args__ = (
        UniqueConstraint("gate_run_id", name="uq_pre_apply_gate_result_gate_run_id"),
        Index("ix_pre_apply_gate_result_created", "created_at"),
        Index("ix_pre_apply_gate_result_recommendation", "recommendation_id", "created_at"),
        # release_id 是 P0-2 阶段 B 加的软关联列：gate 跑完时可能还没有对应
        # release（gate 是 apply 的前置），因此可为空；release 创建成功后由
        # apply 流程回填，用于按 release 维度审计 gate 链路。
        Index("ix_pre_apply_gate_result_release", "release_id", "created_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    gate_run_id = Column(String(128), nullable=False, unique=True)
    recommendation_id = Column(String(128), nullable=False)
    release_id = Column(String(128))
    allow_apply = Column(Boolean, nullable=False, server_default=text("false"))
    gate_status = Column(String(32), nullable=False)
    total_checks = Column(Integer, nullable=False, server_default=text("0"))
    passed_checks = Column(Integer, nullable=False, server_default=text("0"))
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ParameterReleaseModel(RdpBase):
    __tablename__ = "parameter_releases"
    __table_args__ = (
        UniqueConstraint("release_id", name="uq_parameter_release_release_id"),
        Index("ix_parameter_release_combo_created", "combo_key", "created_at"),
        Index("ix_parameter_release_recommendation", "recommendation_id"),
        ForeignKeyConstraint(
            ["parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_param_release_ps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_param_release_prev_ps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "apply_result IN ('pending', 'blocked_by_gate', 'success', 'failed')",
            name="ck_release_apply_result",
        ),
        CheckConstraint(
            "observation_status IN ('pending', 'observing', 'completed', "
            "'rollback_recommended', 'rolled_back')",
            name="ck_release_observation_status",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    release_id = Column(String(128), nullable=False, unique=True)
    family = Column(String(64), nullable=False)
    timeframe = Column(String(16), nullable=False)
    combo_key = Column(String(128), nullable=False)
    recommendation_id = Column(String(128), nullable=False)
    parameter_set_id = Column(String(128), nullable=False)
    previous_parameter_set_id = Column(String(128))
    actor = Column(String(128), nullable=False, server_default=text("'operator'"))
    gate_result_ref = Column(String(128))
    gate_status = Column(String(32))
    apply_result = Column(String(32), nullable=False, server_default=text("'pending'"))
    observation_status = Column(String(32), nullable=False, server_default=text("'pending'"))
    observation_window_hours = Column(Integer, nullable=False, server_default=text("24"))
    notes = Column(Text)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ObservationResultModel(RdpBase):
    __tablename__ = "observation_results"
    __table_args__ = (
        UniqueConstraint("release_id", name="uq_observation_result_release_id"),
        Index("ix_observation_result_combo_eval", "combo_key", "evaluated_at"),
        CheckConstraint(
            "status IN ('observing', 'completed', 'rollback_recommended')",
            name="ck_obs_status",
        ),
        CheckConstraint(
            "recommendation IN ('keep', 'review', 'rollback_recommended')",
            name="ck_obs_recommendation",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    release_id = Column(String(128), nullable=False, unique=True)
    family = Column(String(64), nullable=False)
    timeframe = Column(String(16), nullable=False)
    combo_key = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False)
    recommendation = Column(String(32), nullable=False)
    observation_window_hours = Column(Integer, nullable=False, server_default=text("24"))
    window_active = Column(Boolean, nullable=False, server_default=text("true"))
    started_at = Column(DateTime(timezone=True))
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RollbackRecommendationModel(RdpBase):
    __tablename__ = "rollback_recommendations"
    __table_args__ = (
        UniqueConstraint("release_id", name="uq_rollback_recommendation_release_id"),
        Index("ix_rollback_recommendation_combo_eval", "combo_key", "evaluated_at"),
        ForeignKeyConstraint(
            ["suggested_target_parameter_set_id"],
            ["governance.parameter_sets.parameter_set_id"],
            name="fk_rollback_rec_target_ps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "severity IN ('none', 'medium', 'high')",
            name="ck_rollback_severity",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    release_id = Column(String(128), nullable=False, unique=True)
    family = Column(String(64), nullable=False)
    timeframe = Column(String(16), nullable=False)
    combo_key = Column(String(128), nullable=False)
    rollback_recommended = Column(Boolean, nullable=False, server_default=text("false"))
    severity = Column(String(32), nullable=False, server_default=text("'none'"))
    suggested_target_parameter_set_id = Column(String(128))
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ReleaseEffectivenessModel(RdpBase):
    __tablename__ = "release_effectiveness"
    __table_args__ = (
        UniqueConstraint("release_id", name="uq_release_effectiveness_release_id"),
        UniqueConstraint("evaluation_id", name="uq_release_effectiveness_evaluation_id"),
        Index("ix_release_effectiveness_combo_eval", "family", "timeframe", "evaluated_at"),
        CheckConstraint(
            "conclusion IN ('rollback_triggered', 'insufficient_evidence', "
            "'ineffective', 'effective', 'mixed')",
            name="ck_release_eff_conclusion",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    evaluation_id = Column(String(128), nullable=False, unique=True)
    release_id = Column(String(128), nullable=False, unique=True)
    family = Column(String(64))
    timeframe = Column(String(16))
    conclusion = Column(String(64), nullable=False)
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class StrategyTuningProposalModel(RdpBase):
    __tablename__ = "strategy_tuning_proposals"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_strategy_tuning_proposal_proposal_id"),
        Index("ix_strategy_tuning_proposal_combo_status", "combo_key", "status"),
        Index("ix_strategy_tuning_proposal_review", "review_id"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(String(128), nullable=False, unique=True)
    review_id = Column(String(128))
    last_review_id = Column(String(128))
    combo_key = Column(String(128), nullable=False)
    family = Column(String(64), nullable=False)
    timeframe = Column(String(16), nullable=False)
    parameter = Column(String(128), nullable=False)
    current_value = Column(JSONB)
    proposed_value = Column(JSONB)
    delta = Column(Double)
    confidence = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    review_required = Column(Boolean, nullable=False, server_default=text("true"))
    dominant_blocker = Column(String(128))
    dominant_blocker_ratio = Column(Double)
    rationale = Column(Text)
    review_notes = Column(Text)
    reviewed_at = Column(DateTime(timezone=True))
    reviewed_by = Column(String(128))
    superseded_at = Column(DateTime(timezone=True))
    superseded_by_review_id = Column(String(128))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DecisionEvidenceBundleModel(RdpBase):
    __tablename__ = "decision_evidence_bundles"
    __table_args__ = (
        UniqueConstraint("round_id", name="uq_decision_evidence_bundle_round_id"),
        Index("ix_decision_evidence_bundle_created", "created_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(String(128), nullable=False, unique=True)
    evidence_summary_path = Column(Text, nullable=False)
    phases_with_data = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    completeness_ratio = Column(Double, nullable=False, server_default=text("0"))
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# =====================================================================
# Schema 创建入口
# =====================================================================

def create_rdp_schema(engine: object) -> None:
    """创建 RDP 的全部 7 个 PostgreSQL schema + 48 张表。

    替代 migrations/research/*.sql 迁移文件。幂等——已存在的 schema/表不会
    被破坏（CREATE SCHEMA IF NOT EXISTS + create_all 的 checkfirst=True）。

    同时执行 governance 表结构迁移（列重命名 / 新增列），确保旧表升级到
    当前 ORM 定义。
    """
    from sqlalchemy import text as _text

    with engine.begin() as conn:  # type: ignore[union-attr]
        for schema in _RDP_SCHEMAS:
            conn.execute(_text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))

    RdpBase.metadata.create_all(engine)  # type: ignore[arg-type]
    _migrate_governance_recommendations(engine)
    _migrate_pre_apply_gate_results(engine)


def _migrate_governance_recommendations(engine: object) -> None:
    """governance.recommendations 表结构迁移（幂等）.

    处理以下变更:
    1. approval_notes → review_notes（语义修正）
    2. 新增 superseded_by 列（记录 supersede 操作人）
    """
    from sqlalchemy import text as _text

    with engine.begin() as conn:  # type: ignore[union-attr]
        # 检查 recommendations 表是否存在
        tbl_exists = conn.execute(_text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'governance' AND table_name = 'recommendations'"
        )).fetchone()
        if not tbl_exists:
            return

        # 1. approval_notes → review_notes
        old_col = conn.execute(_text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'governance' AND table_name = 'recommendations' "
            "AND column_name = 'approval_notes'"
        )).fetchone()
        new_col = conn.execute(_text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'governance' AND table_name = 'recommendations' "
            "AND column_name = 'review_notes'"
        )).fetchone()
        if old_col and not new_col:
            conn.execute(_text(
                "ALTER TABLE governance.recommendations "
                "RENAME COLUMN approval_notes TO review_notes"
            ))

        # 2. 新增 superseded_by 列
        conn.execute(_text(
            "ALTER TABLE governance.recommendations "
            "ADD COLUMN IF NOT EXISTS superseded_by VARCHAR(128)"
        ))


def _migrate_pre_apply_gate_results(engine: object) -> None:
    """governance.pre_apply_gate_results 表结构迁移（幂等）。

    P0-2 阶段 B 新增：
      1. release_id VARCHAR(128) NULL — 软关联 parameter_releases.release_id，
         用于按 release 维度审计 gate 链路。gate 跑完时可能还没 release（gate
         是 apply 的前置），因此列可空，由 apply 流程回填。不加 FK，避免
         recommendation 被 supersede 时阻塞清理。
      2. ix_pre_apply_gate_result_release 索引 — 加速按 release_id 的查询，
         待 `db_list_gate_results_for_release` 切到 release_id 直查后生效。
    """
    from sqlalchemy import text as _text

    with engine.begin() as conn:  # type: ignore[union-attr]
        tbl_exists = conn.execute(_text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'governance' AND table_name = 'pre_apply_gate_results'"
        )).fetchone()
        if not tbl_exists:
            return

        conn.execute(_text(
            "ALTER TABLE governance.pre_apply_gate_results "
            "ADD COLUMN IF NOT EXISTS release_id VARCHAR(128)"
        ))
        conn.execute(_text(
            "CREATE INDEX IF NOT EXISTS ix_pre_apply_gate_result_release "
            "ON governance.pre_apply_gate_results (release_id, created_at DESC)"
        ))
