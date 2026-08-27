"""RDP (Research Data Platform) SQLAlchemy ORM 模型。

替代 migrations/research/*.sql，通过 RdpBase.metadata.create_all() 自动建表。
当前 102 张表分布在 7 个 PostgreSQL schema：meta / staging / bronze / silver /
gold / research / governance。

设计决策：
- 静态 ORM class 用于结构独特的 meta / research / governance 表
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
# META Schema — 14 张表
# =====================================================================

class DatasetManifestModel(RdpBase):
    __tablename__ = "dataset_manifests"
    __table_args__ = (
        Index("idx_dm_layer_domain", "dataset_layer", "dataset_domain", "instrument_type", "timeframe"),
        Index("idx_dm_version", "dataset_version"),
        Index("idx_dm_status", "status"),
        CheckConstraint("dataset_layer IN ('staging','bronze','silver','gold')", name="chk_dm_layer"),
        CheckConstraint("dataset_domain IN ('candles','funding','microstructure')", name="chk_dm_domain"),
        CheckConstraint("instrument_type IN ('spot','swap')", name="chk_dm_inst"),
        CheckConstraint("source_type IN ('historical_file','api','api_stream','derived')", name="chk_dm_source"),
        CheckConstraint("status IN ('active','superseded','building','failed','dormant')", name="chk_dm_status"),
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
        CheckConstraint("dataset_domain IN ('candles','funding','microstructure')", name="chk_rsf_domain"),
        CheckConstraint("source_type IN ('historical_file','api_snapshot','api_stream')", name="chk_rsf_source"),
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
        CheckConstraint("dataset_domain IN ('candles','funding','microstructure')", name="chk_ir_domain"),
        CheckConstraint("status IN ('pending','running','succeeded','failed','retrying','backfilling')", name="chk_ir_status"),
        CheckConstraint("trigger_mode IN ('scheduler','manual','auto_gap_repair','daemon')", name="chk_ir_trigger"),
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
        CheckConstraint("dataset_domain IN ('candles','funding','microstructure')", name="chk_iri_domain"),
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
        CheckConstraint("dataset_domain IN ('candles','funding','microstructure')", name="chk_cp_domain"),
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
        CheckConstraint("dataset_domain IN ('candles','funding','microstructure')", name="chk_qr_domain"),
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

    承载 OKX public `liquidation-orders` 频道推送的每条 details 行。官方当前
    没有可重建遗漏事件的公共 REST 历史接口；本表只能证明 AATS 实际观测到的
    窗口，启动前或中断期间保持 unknown / awaiting_live_collection。

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
        Index("ix_raw_liquidations_scope_inst_ts", "source_scope", "inst_id", "ts"),
        Index("ix_raw_liquidations_received", "received_at"),
        CheckConstraint("side IN ('buy','sell')", name="chk_raw_liq_side"),
        CheckConstraint(
            "source_scope IN ('fixed_trading_scope','broad_market_context')",
            name="chk_raw_liq_source_scope",
        ),
        CheckConstraint(
            "raw_payload_hash IS NULL OR "
            "(length(raw_payload_hash) = 64 "
            "AND raw_payload_hash = lower(raw_payload_hash))",
            name="chk_raw_liq_payload_hash",
        ),
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
    raw_payload_hash = Column(String(64))
    source_scope = Column(
        Text,
        nullable=False,
        server_default=text("'broad_market_context'"),
    )
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# =====================================================================
# BRONZE / STAGING — P1-D Phase 1A microstructure 表 (§6)
# =====================================================================
# 4 张表供 OKX `trades` / `bbo-tbt` / `books5` / open-interest-funding-mark
# 三大 WS 频道落库。参考 docs/design/p1d_phase1a_implementation_design_2026_04_20.md。
# 实际 DDL 由 migrations/batch_b_05_microstructure.sql 承载,ORM 仅供 create_all
# 兜底 + 单元测试 + 程序化读写。

class BronzeMarketTradesModel(RdpBase):
    """bronze.market_trades — OKX public trades WS 频道落库。

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


class BronzeMarketOrderbookPayloadModel(RdpBase):
    """bronze.market_orderbook_payloads — orderbook payload truth sidecar.

    This is schema-only execution-science infrastructure. Runtime collector
    writes are intentionally not connected by this model. The table stores
    payload and collector-sequence evidence next to existing bbo/books5 snapshot
    rows without duplicating that truth into the execution database.
    """

    __tablename__ = "market_orderbook_payloads"
    __table_args__ = (
        PrimaryKeyConstraint(
            "snapshot_table",
            "symbol",
            "ts",
            "row_checksum",
            name="pk_brz_orderbook_payloads",
        ),
        Index("idx_brz_orderbook_payloads_snapshot", "snapshot_table", "symbol", "ts"),
        Index(
            "idx_brz_orderbook_payloads_sequence",
            "snapshot_table",
            "symbol",
            "collector_sequence",
        ),
        Index(
            "idx_brz_orderbook_payloads_source_ts",
            "snapshot_table",
            "symbol",
            "source_ts",
        ),
        CheckConstraint(
            "storage_table = 'bronze.market_orderbook_payloads'",
            name="chk_brz_orderbook_payload_storage_table",
        ),
        CheckConstraint(
            "snapshot_table IN ("
            "'bronze.market_orderbook_bbo',"
            "'bronze.market_orderbook_books5'"
            ")",
            name="chk_brz_orderbook_payload_snapshot_table",
        ),
        CheckConstraint(
            "collector_sequence > 0",
            name="chk_brz_orderbook_payload_collector_sequence",
        ),
        CheckConstraint(
            "collector_sequence_scope = 'per_ingest_run_symbol_channel'",
            name="chk_brz_orderbook_payload_sequence_scope",
        ),
        CheckConstraint(
            "length(row_checksum) = 71 AND row_checksum LIKE 'sha256:%'",
            name="chk_brz_orderbook_payload_row_checksum",
        ),
        CheckConstraint(
            "checksum_version = 'orderbook_row_v1'",
            name="chk_brz_orderbook_payload_checksum_version",
        ),
        CheckConstraint(
            "capture_status IN ("
            "'snapshot_only_diff_payload_missing',"
            "'diff_payload_persisted',"
            "'diff_payload_unavailable'"
            ")",
            name="chk_brz_orderbook_payload_capture_status",
        ),
        CheckConstraint(
            "payload_hash IS NULL OR "
            "(length(payload_hash) = 71 AND payload_hash LIKE 'sha256:%')",
            name="chk_brz_orderbook_payload_hash",
        ),
        CheckConstraint(
            "previous_payload_hash IS NULL OR "
            "(length(previous_payload_hash) = 71 "
            "AND previous_payload_hash LIKE 'sha256:%')",
            name="chk_brz_orderbook_payload_previous_hash",
        ),
        CheckConstraint(
            "payload_schema_version IS NULL OR "
            "payload_schema_version = 'orderbook_diff_payload_v1'",
            name="chk_brz_orderbook_payload_schema_version",
        ),
        CheckConstraint(
            "capture_status <> 'diff_payload_persisted' OR ("
            "payload_hash IS NOT NULL "
            "AND payload_schema_version = 'orderbook_diff_payload_v1' "
            "AND payload_kind IS NOT NULL "
            "AND raw_payload IS NOT NULL"
            ")",
            name="chk_brz_orderbook_payload_diff_required",
        ),
        {"schema": "bronze"},
    )

    storage_table = Column(
        Text,
        nullable=False,
        server_default=text("'bronze.market_orderbook_payloads'"),
    )
    snapshot_table = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    source_ts = Column(DateTime(timezone=True), nullable=False)
    collector_sequence = Column(BigInteger, nullable=False)
    collector_sequence_scope = Column(
        Text,
        nullable=False,
        server_default=text("'per_ingest_run_symbol_channel'"),
    )
    row_checksum = Column(Text, nullable=False)
    checksum_version = Column(
        Text,
        nullable=False,
        server_default=text("'orderbook_row_v1'"),
    )
    capture_status = Column(Text, nullable=False)
    payload_hash = Column(Text)
    payload_schema_version = Column(Text)
    payload_kind = Column(Text)
    raw_payload = Column(JSONB)
    exchange_sequence_id = Column(Text)
    previous_payload_hash = Column(Text)
    channel = Column(Text)
    capture_reason = Column(Text)
    missing_evidence = Column(JSONB)
    ingest_run_id = Column(UUID(as_uuid=False), ForeignKey("meta.ingest_runs.ingest_run_id"), nullable=False)
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
# SILVER — P1-D Phase 1A microstructure 15m 聚合表 (§5)
# =====================================================================
# 5 张 Silver 15m 表,由 microstructure_silver_merger 从 Bronze/staging
# 聚合产出。实际 DDL 由 migrations/batch_b_06_silver_microstructure.sql 承载;
# ORM 仅供 create_all 兜底 + 单元测试 + 程序化读写。
#
# 共用规范 (§5 开头):
#   - schema = silver
#   - PK = (symbol, ts), ts = 15m bar 起点 (UTC 对齐)
#   - footer: ingest_run_id / dataset_version / quality_flags / created_at /
#             updated_at
#   - quality_flags TEXT[] 合法值:
#       etl_failed, partial_data, gap_filled_with_nulls, stale_source,
#       whale_threshold_reinit, ema_seed_from_sma, partial_baseline,
#       orderbook_bbo_no_data, orderbook_books5_no_data,
#       trades_no_data, oi_no_data, funding_no_data, mark_no_data,
#       liquidation_no_data


class SilverMarketOrderbookMetrics15mModel(RdpBase):
    """silver.market_orderbook_metrics_15m — §5.1.

    来源 bronze.market_orderbook_bbo (1Hz) + bronze.market_orderbook_books5
    (2Hz),聚合为 15m 窗口统计 + depth + imbalance。
    """
    __tablename__ = "market_orderbook_metrics_15m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_slv_micro_orderbook_15m"),
        Index("idx_slv_micro_orderbook_15m_ts", "ts"),
        Index("idx_slv_micro_orderbook_15m_ver", "dataset_version"),
        {"schema": "silver"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    # BBO level
    bbo_imbalance_mean = Column(Numeric(12, 8))
    bbo_imbalance_std = Column(Numeric(12, 8))
    bbo_imbalance_last = Column(Numeric(12, 8))
    bbo_samples_n = Column(Integer, nullable=False, server_default=text("0"))
    # Top-5 level
    top5_bid_depth_ccy = Column(Numeric(28, 10))
    top5_ask_depth_ccy = Column(Numeric(28, 10))
    top5_imbalance_mean = Column(Numeric(12, 8))
    top5_imbalance_ema = Column(Numeric(12, 8))
    top5_weighted_imbalance = Column(Numeric(12, 8))
    books5_samples_n = Column(Integer, nullable=False, server_default=text("0"))
    # Spread
    spread_bps_mean = Column(Numeric(12, 4))
    spread_bps_max = Column(Numeric(12, 4))
    spread_bps_min = Column(Numeric(12, 4))
    # Mid anchor
    mid_price_last = Column(Numeric(20, 10))
    # Footer
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    dataset_version = Column(Text, nullable=False)
    quality_flags = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


class SilverMarketTradeFlow15mModel(RdpBase):
    """silver.market_trade_flow_15m — §5.2.

    来源 bronze.market_trades;聚合 volume + taker buy/sell split + whale
    detection + VWAP 相对 mid 的偏移。
    """
    __tablename__ = "market_trade_flow_15m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_slv_micro_trade_flow_15m"),
        Index("idx_slv_micro_trade_flow_15m_ts", "ts"),
        Index("idx_slv_micro_trade_flow_15m_ver", "dataset_version"),
        {"schema": "silver"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    # Volume
    total_volume_ccy = Column(Numeric(28, 10))
    buy_volume_ccy = Column(Numeric(28, 10))
    sell_volume_ccy = Column(Numeric(28, 10))
    trade_count = Column(Integer, nullable=False, server_default=text("0"))
    # Aggressor flow
    taker_buy_ratio = Column(Numeric(12, 8))
    trade_flow_imbalance = Column(Numeric(12, 8))
    log_tfi = Column(Numeric(12, 8))
    # Size distribution
    mean_trade_size = Column(Numeric(18, 8))
    p50_trade_size = Column(Numeric(18, 8))
    p95_trade_size = Column(Numeric(18, 8))
    p99_trade_size = Column(Numeric(18, 8))
    max_trade_size = Column(Numeric(18, 8))
    # Whale detection
    whale_threshold_applied = Column(Numeric(18, 8))
    whale_count = Column(Integer, nullable=False, server_default=text("0"))
    whale_buy_volume_ccy = Column(Numeric(28, 10))
    whale_sell_volume_ccy = Column(Numeric(28, 10))
    whale_direction = Column(Numeric(12, 8))
    # Aggressiveness
    vwap = Column(Numeric(20, 10))
    mid_price_ref = Column(Numeric(20, 10))
    vwap_minus_mid_bps = Column(Numeric(12, 4))
    # Footer
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    dataset_version = Column(Text, nullable=False)
    quality_flags = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


class SilverMarketOiFundingMetrics15mModel(RdpBase):
    """silver.market_oi_funding_metrics_15m — §5.3.

    来源 staging.market_oi_funding_ticks (tick_type∈{oi,funding,mark}) +
    silver.market_orderbook_metrics_15m 的 mid_price_last;聚合 OI 四价 /
    EMA-20 / price-OI regime / funding z-score 7d / basis bps。
    """
    __tablename__ = "market_oi_funding_metrics_15m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_slv_micro_oi_funding_15m"),
        Index("idx_slv_micro_oi_funding_15m_ts", "ts"),
        Index("idx_slv_micro_oi_funding_15m_ver", "dataset_version"),
        {"schema": "silver"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    # OI
    oi_open = Column(Numeric(28, 10))
    oi_close = Column(Numeric(28, 10))
    oi_high = Column(Numeric(28, 10))
    oi_low = Column(Numeric(28, 10))
    oi_delta = Column(Numeric(18, 10))
    oi_samples_n = Column(Integer, nullable=False, server_default=text("0"))
    # EMA-20
    oi_ema_20 = Column(Numeric(28, 10))
    oi_delta_vs_ema = Column(Numeric(18, 10))
    # Price-OI joint regime
    price_change_bps = Column(Numeric(12, 4))
    oi_price_regime = Column(Text)
    # Funding
    funding_rate_current = Column(Numeric(18, 12))
    funding_rate_next_est = Column(Numeric(18, 12))
    funding_z_score_7d = Column(Numeric(12, 6))
    funding_deviation_30d = Column(Numeric(18, 12))
    minutes_to_next_funding = Column(Integer)
    # Mark / basis
    mark_price = Column(Numeric(20, 10))
    mid_price_ref = Column(Numeric(20, 10))
    basis_bps = Column(Numeric(12, 4))
    # Footer
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    dataset_version = Column(Text, nullable=False)
    quality_flags = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


class SilverMarketVolumeProfile15mModel(RdpBase):
    """silver.market_volume_profile_15m — §5.4.

    来源本 bar 的 volume_ccy (从 silver.market_trade_flow_15m 或直接读
    bronze.market_trades 聚合) + 历史同时段 4-week rolling baseline
    (同 dow × hod × 15min slot);冷启动阶段 baseline_sample_weeks < 4
    时 z_score=NULL + quality_flags += 'partial_baseline'。
    """
    __tablename__ = "market_volume_profile_15m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_slv_micro_volume_profile_15m"),
        Index("idx_slv_micro_volume_profile_15m_ts", "ts"),
        Index("idx_slv_micro_volume_profile_15m_ver", "dataset_version"),
        {"schema": "silver"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    # 本 bar
    volume_ccy = Column(Numeric(28, 10))
    trade_count = Column(Integer, nullable=False, server_default=text("0"))
    # Seasonal baseline
    expected_volume_ccy = Column(Numeric(28, 10))
    expected_volume_std = Column(Numeric(28, 10))
    volume_z_score = Column(Numeric(12, 6))
    volume_spike_flag = Column(
        Boolean, nullable=False, server_default=text("FALSE"),
    )
    dow_hod_slot = Column(Text)
    # Interaction
    vol_weighted_tfi = Column(Numeric(28, 10))
    # Cold-start diagnostic
    baseline_sample_weeks = Column(
        Integer, nullable=False, server_default=text("0"),
    )
    # Footer
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    dataset_version = Column(Text, nullable=False)
    quality_flags = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


class SilverMarketLiquidationMetrics15mModel(RdpBase):
    """silver.market_liquidation_metrics_15m — §5.5.

    来源 staging.raw_liquidations (inst_id = symbol 直接映射);聚合
    long/short counts + notional + cascade detection + 7d z-score。
    与 staging.raw_liquidations 的 "side" 字段 convention 对齐:
    OKX liquidation-orders 中 side 表示被清算方向(长仓被清算 → side='sell',
    短仓被清算 → side='buy'),具体映射见 §5.5 注释与 _build_liquidation_metrics。
    """
    __tablename__ = "market_liquidation_metrics_15m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_slv_micro_liq_metrics_15m"),
        Index("idx_slv_micro_liq_metrics_15m_ts", "ts"),
        Index("idx_slv_micro_liq_metrics_15m_ver", "dataset_version"),
        {"schema": "silver"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    # Counts
    long_liq_count = Column(Integer, nullable=False, server_default=text("0"))
    short_liq_count = Column(Integer, nullable=False, server_default=text("0"))
    # Notional
    long_liq_notional_usd = Column(Numeric(28, 10))
    short_liq_notional_usd = Column(Numeric(28, 10))
    liq_imbalance = Column(Numeric(12, 8))
    max_single_liq_usd = Column(Numeric(28, 10))
    # Cascade detection
    cascade_flag = Column(
        Boolean, nullable=False, server_default=text("FALSE"),
    )
    cascade_threshold_used = Column(Integer)
    intensity_z_7d = Column(Numeric(12, 6))
    # Footer
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    dataset_version = Column(Text, nullable=False)
    quality_flags = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )


# =====================================================================
# BRONZE — P1-D Stage 5 OKX REST 历史回填 bronze 表 (§batch_b_08, §batch_b_09)
# =====================================================================
# 3 张 Bronze 表, 由 OKX REST backfill collectors (aats/data_platform/collectors/
# backfill/okx_rest_history_collectors.py) 批量回填. 实际 DDL 由 migrations/
# batch_b_08_oi_history.sql + batch_b_09_mark_ls_history.sql 承载; ORM 仅供
# create_all 兜底 + 单元测试 + 程序化读写.
#
# 参考: docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md
#       §3.1 (OI history) / §3.2 (mark-price) / §3.3 (LS ratio)


class BronzeMarketOIHistory1hModel(RdpBase):
    """bronze.market_oi_history_1h — OKX REST open-interest-history 回填.

    来源: /api/v5/rubik/stat/contracts/open-interest-history (period=1H).
    natural PK: (symbol, ts) — 每 1h bar 起点唯一.
    UPSERT 幂等: INSERT ... ON CONFLICT (PK) DO NOTHING (历史 OI 不会变).

    oi 是 OKX 返回的 OI 张数 (contracts); oi_ccy 是基础货币量; oi_usd 保留列
    但本 stage 不填 (OKX 另一 endpoint `open-interest-usd` 提供, 后续 Silver
    ETL 可 join mark_price × oi_ccy 推导).
    """
    __tablename__ = "market_oi_history_1h"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_brz_oi_history_1h"),
        Index("idx_brz_oi_history_1h_ts", "ts"),
        Index("idx_brz_oi_history_1h_sym_ts", "symbol", "ts"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # 1h bar 起点 (UTC)
    oi = Column(Numeric(28, 10), nullable=False)                     # OKX `oi` 张数
    oi_ccy = Column(Numeric(28, 10))                                 # OKX `oiCcy` 基础货币量
    oi_usd = Column(Numeric(28, 10))                                 # OKX `oiUsd` (optional)
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class BronzeMarketMarkPriceCandles1mModel(RdpBase):
    """bronze.market_mark_price_candles_1m — OKX REST mark-price-candles-history 回填.

    来源: /api/v5/market/mark-price-candles-history (period=1m).
    natural PK: (symbol, ts).
    5 个价列 open/high/low/close 全 required (OKX 返回 confirm=1 的 bar).
    """
    __tablename__ = "market_mark_price_candles_1m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_brz_mark_candles_1m"),
        Index("idx_brz_mark_candles_1m_ts", "ts"),
        Index("idx_brz_mark_candles_1m_sym_ts", "symbol", "ts"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # 1m bar 起点 (UTC)
    open = Column(Numeric(20, 10), nullable=False)
    high = Column(Numeric(20, 10), nullable=False)
    low = Column(Numeric(20, 10), nullable=False)
    close = Column(Numeric(20, 10), nullable=False)
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class StagingOfficialTradeHistoryModel(RdpBase):
    """官方 REST/bulk 历史逐笔成交，来源与 live capture 不混列。"""

    __tablename__ = "official_trade_history"
    __table_args__ = (
        PrimaryKeyConstraint(
            "source_id", "symbol", "ts", "trade_id",
            name="pk_stg_official_trade_history",
        ),
        Index("idx_stg_official_trade_history_sym_ts", "symbol", "ts"),
        Index("idx_stg_official_trade_history_sha", "raw_partition_sha256"),
        CheckConstraint("side IN ('buy','sell')", name="chk_stg_official_trade_side"),
        CheckConstraint("px > 0 AND sz > 0", name="chk_stg_official_trade_values"),
        CheckConstraint(
            "length(raw_partition_sha256) = 64 "
            "AND raw_partition_sha256 = lower(raw_partition_sha256)",
            name="chk_stg_official_trade_sha",
        ),
        {"schema": "staging"},
    )

    source_id = Column(UUID(as_uuid=False), ForeignKey("meta.data_source_registry.source_id"), nullable=False)
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    trade_id = Column(Text, nullable=False)
    px = Column(Numeric(20, 10), nullable=False)
    sz = Column(Numeric(28, 10), nullable=False)
    side = Column(Text, nullable=False)
    source_order_type = Column(Text)
    raw_payload = Column(JSONB, nullable=False)
    raw_partition_sha256 = Column(String(64), nullable=False)
    ingest_run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.ingest_runs.ingest_run_id"),
        nullable=False,
    )
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class StagingOfficialL2HistoryModel(RdpBase):
    """官方高分辨率 L2 原始 snapshot/update 事件。"""

    __tablename__ = "official_l2_history"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "symbol", "ts", "source_row_hash",
            name="uq_stg_official_l2_row",
        ),
        Index("idx_stg_official_l2_sym_ts", "symbol", "ts"),
        Index("idx_stg_official_l2_sequence", "symbol", "sequence_id"),
        CheckConstraint("action IN ('snapshot','update')", name="chk_stg_official_l2_action"),
        CheckConstraint(
            "length(source_row_hash) = 64 "
            "AND source_row_hash = lower(source_row_hash)",
            name="chk_stg_official_l2_row_hash",
        ),
        CheckConstraint(
            "length(raw_partition_sha256) = 64 "
            "AND raw_partition_sha256 = lower(raw_partition_sha256)",
            name="chk_stg_official_l2_sha",
        ),
        {"schema": "staging"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_id = Column(UUID(as_uuid=False), ForeignKey("meta.data_source_registry.source_id"), nullable=False)
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    sequence_id = Column(BigInteger)
    previous_sequence_id = Column(BigInteger)
    action = Column(Text, nullable=False)
    bids = Column(JSONB, nullable=False)
    asks = Column(JSONB, nullable=False)
    checksum = Column(Text)
    source_row_hash = Column(String(64), nullable=False)
    raw_partition_sha256 = Column(String(64), nullable=False)
    ingest_run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.ingest_runs.ingest_run_id"),
        nullable=False,
    )
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class BronzeHistoricalOrderbookBbo1hzModel(RdpBase):
    """okx_bulk L2 因果重采样的 1 Hz BBO；不能证明 live capture。"""

    __tablename__ = "historical_orderbook_bbo_1hz"
    __table_args__ = (
        PrimaryKeyConstraint("bundle_id", "symbol", "ts", name="pk_brz_hist_bbo_1hz"),
        Index("idx_brz_hist_bbo_1hz_sym_ts", "symbol", "ts"),
        CheckConstraint("source_state_ts <= ts", name="chk_brz_hist_bbo_no_future"),
        CheckConstraint("staleness_ms >= 0", name="chk_brz_hist_bbo_staleness"),
        CheckConstraint(
            "bid_px > 0 AND bid_sz > 0 AND ask_px > bid_px AND ask_sz > 0",
            name="chk_brz_hist_bbo_values",
        ),
        CheckConstraint(
            "source_label = 'okx_bulk_l2_resampled'",
            name="chk_brz_hist_bbo_source_label",
        ),
        {"schema": "bronze"},
    )

    bundle_id = Column(UUID(as_uuid=False), ForeignKey("meta.dataset_bundles.bundle_id"), nullable=False)
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    source_state_ts = Column(DateTime(timezone=True), nullable=False)
    staleness_ms = Column(Integer, nullable=False)
    bid_px = Column(Numeric(20, 10), nullable=False)
    bid_sz = Column(Numeric(28, 10), nullable=False)
    ask_px = Column(Numeric(20, 10), nullable=False)
    ask_sz = Column(Numeric(28, 10), nullable=False)
    source_label = Column(Text, nullable=False, server_default=text("'okx_bulk_l2_resampled'"))
    transform_version = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class BronzeHistoricalOrderbookBooks5_2hzModel(RdpBase):
    """okx_bulk L2 因果重采样的 2 Hz books5 JSON payload。"""

    __tablename__ = "historical_orderbook_books5_2hz"
    __table_args__ = (
        PrimaryKeyConstraint("bundle_id", "symbol", "ts", name="pk_brz_hist_books5_2hz"),
        Index("idx_brz_hist_books5_2hz_sym_ts", "symbol", "ts"),
        CheckConstraint("source_state_ts <= ts", name="chk_brz_hist_books5_no_future"),
        CheckConstraint("staleness_ms >= 0", name="chk_brz_hist_books5_staleness"),
        CheckConstraint(
            "source_label = 'okx_bulk_l2_resampled'",
            name="chk_brz_hist_books5_source_label",
        ),
        {"schema": "bronze"},
    )

    bundle_id = Column(UUID(as_uuid=False), ForeignKey("meta.dataset_bundles.bundle_id"), nullable=False)
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    source_state_ts = Column(DateTime(timezone=True), nullable=False)
    staleness_ms = Column(Integer, nullable=False)
    bids = Column(JSONB, nullable=False)
    asks = Column(JSONB, nullable=False)
    source_label = Column(Text, nullable=False, server_default=text("'okx_bulk_l2_resampled'"))
    transform_version = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _make_mark_proxy_model(timeframe: str) -> type:
    table_name = f"market_mark_price_candles_{timeframe}"
    attrs: dict[str, object] = {
        "__tablename__": table_name,
        "__table_args__": (
            PrimaryKeyConstraint("source_id", "symbol", "ts", name=f"pk_brz_mark_proxy_{timeframe}"),
            Index(f"idx_brz_mark_proxy_{timeframe}_sym_ts", "symbol", "ts"),
            CheckConstraint(
                "length(raw_partition_sha256) = 64 "
                "AND raw_partition_sha256 = lower(raw_partition_sha256)",
                name=f"chk_brz_mark_proxy_{timeframe}_sha",
            ),
            CheckConstraint(
                "open > 0 AND high > 0 AND low > 0 AND close > 0 "
                "AND high >= open AND high >= close AND high >= low "
                "AND low <= open AND low <= close",
                name=f"chk_brz_mark_proxy_{timeframe}_ohlc",
            ),
            CheckConstraint(
                "confirm IS TRUE",
                name=f"chk_brz_mark_proxy_{timeframe}_confirmed",
            ),
            CheckConstraint(
                "source_label = 'bar_proxy'",
                name=f"chk_brz_mark_proxy_{timeframe}_source_label",
            ),
            {"schema": "bronze"},
        ),
        "source_id": Column(UUID(as_uuid=False), ForeignKey("meta.data_source_registry.source_id"), nullable=False),
        "symbol": Column(Text, nullable=False),
        "ts": Column(DateTime(timezone=True), nullable=False),
        "open": Column(Numeric(20, 10), nullable=False),
        "high": Column(Numeric(20, 10), nullable=False),
        "low": Column(Numeric(20, 10), nullable=False),
        "close": Column(Numeric(20, 10), nullable=False),
        "confirm": Column(Boolean, nullable=False),
        "source_label": Column(Text, nullable=False, server_default=text("'bar_proxy'")),
        "raw_partition_sha256": Column(String(64), nullable=False),
        "ingest_run_id": Column(
            UUID(as_uuid=False),
            ForeignKey("meta.ingest_runs.ingest_run_id"),
            nullable=False,
        ),
        "received_at": Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    }
    model = type(f"BronzeMarketMarkPriceCandles{timeframe}ProxyModel", (RdpBase,), attrs)
    _data_layer_models[f"bronze.{table_name}"] = model
    return model


BronzeMarketMarkPriceCandles15mProxyModel = _make_mark_proxy_model("15m")
BronzeMarketMarkPriceCandles1hProxyModel = _make_mark_proxy_model("1h")


class SilverHistoricalOrderbookMetrics15mModel(RdpBase):
    """Bundle-scoped historical L2 metrics; never presented as live capture."""

    __tablename__ = "historical_orderbook_metrics_15m"
    __table_args__ = (
        PrimaryKeyConstraint(
            "bundle_id",
            "symbol",
            "ts",
            name="pk_slv_hist_orderbook_metrics_15m",
        ),
        Index("idx_slv_hist_orderbook_metrics_sym_ts", "symbol", "ts"),
        CheckConstraint(
            "bbo_samples_n > 0 AND books5_samples_n >= 0",
            name="chk_slv_hist_orderbook_counts",
        ),
        CheckConstraint(
            "max_staleness_ms >= 0",
            name="chk_slv_hist_orderbook_staleness",
        ),
        CheckConstraint(
            "mid_price_mean > 0 AND spread_bps_mean >= 0 "
            "AND top_imbalance_mean >= -1 AND top_imbalance_mean <= 1",
            name="chk_slv_hist_orderbook_values",
        ),
        CheckConstraint(
            "source_max_ts >= ts",
            name="chk_slv_hist_orderbook_source_time",
        ),
        CheckConstraint(
            "length(output_fingerprint) = 64 "
            "AND output_fingerprint = lower(output_fingerprint)",
            name="chk_slv_hist_orderbook_fingerprint",
        ),
        {"schema": "silver"},
    )

    bundle_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.dataset_bundles.bundle_id"),
        nullable=False,
    )
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    bbo_samples_n = Column(Integer, nullable=False)
    books5_samples_n = Column(Integer, nullable=False)
    mid_price_mean = Column(Numeric(28, 12), nullable=False)
    spread_bps_mean = Column(Numeric(28, 12), nullable=False)
    top_imbalance_mean = Column(Numeric(28, 12), nullable=False)
    max_staleness_ms = Column(Integer, nullable=False)
    source_max_ts = Column(DateTime(timezone=True), nullable=False)
    transform_version = Column(Text, nullable=False)
    output_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class SilverHistoricalTradeFlow15mModel(RdpBase):
    """Source-aware historical trade-flow metrics scoped by dataset bundle."""

    __tablename__ = "historical_trade_flow_15m"
    __table_args__ = (
        PrimaryKeyConstraint(
            "bundle_id",
            "symbol",
            "ts",
            name="pk_slv_hist_trade_flow_15m",
        ),
        Index("idx_slv_hist_trade_flow_sym_ts", "symbol", "ts"),
        CheckConstraint(
            "trade_count > 0 AND buy_count >= 0 AND sell_count >= 0 "
            "AND trade_count = buy_count + sell_count",
            name="chk_slv_hist_trade_counts",
        ),
        CheckConstraint(
            "total_size > 0 AND buy_size >= 0 AND sell_size >= 0 "
            "AND total_size = buy_size + sell_size AND vwap > 0 "
            "AND trade_flow_imbalance >= -1 AND trade_flow_imbalance <= 1",
            name="chk_slv_hist_trade_values",
        ),
        CheckConstraint(
            "source_max_ts >= ts",
            name="chk_slv_hist_trade_source_time",
        ),
        CheckConstraint(
            "length(output_fingerprint) = 64 "
            "AND output_fingerprint = lower(output_fingerprint)",
            name="chk_slv_hist_trade_fingerprint",
        ),
        {"schema": "silver"},
    )

    bundle_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.dataset_bundles.bundle_id"),
        nullable=False,
    )
    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    trade_count = Column(Integer, nullable=False)
    buy_count = Column(Integer, nullable=False)
    sell_count = Column(Integer, nullable=False)
    total_size = Column(Numeric(38, 18), nullable=False)
    buy_size = Column(Numeric(38, 18), nullable=False)
    sell_size = Column(Numeric(38, 18), nullable=False)
    vwap = Column(Numeric(28, 12), nullable=False)
    trade_flow_imbalance = Column(Numeric(28, 12), nullable=False)
    source_max_ts = Column(DateTime(timezone=True), nullable=False)
    transform_version = Column(Text, nullable=False)
    output_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class BronzeMarketLongShortRatio5mModel(RdpBase):
    """bronze.market_long_short_ratio_5m — OKX REST long-short-account-ratio 回填.

    来源: /api/v5/rubik/stat/contracts/long-short-account-ratio (period=5m).
    natural PK: (symbol, ts).
    OKX endpoint 以 `ccy` 为参数 (e.g. "BTC"), collector 在写入时规范化为
    "{ccy}-USDT-SWAP" 以保持统一 symbol schema.

    LS ratio 有两种:
      - ls_ratio_positions: position-size based (部分市场/时段提供)
      - ls_ratio_accounts:  account-count based (OKX long-short-account-ratio 主字段)
    两列都 nullable, collector 按实际返回填.
    """
    __tablename__ = "market_long_short_ratio_5m"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="pk_brz_ls_ratio_5m"),
        Index("idx_brz_ls_ratio_5m_ts", "ts"),
        Index("idx_brz_ls_ratio_5m_sym_ts", "symbol", "ts"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # 5m bar 起点 (UTC)
    ls_ratio_positions = Column(Numeric(18, 10))
    ls_ratio_accounts = Column(Numeric(18, 10))
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class BronzeMarketLongShortRatio1hModel(RdpBase):
    """bronze.market_long_short_ratio_1h — OKX REST long-short-account-ratio 回填.

    来源: /api/v5/rubik/stat/contracts/long-short-account-ratio (period=1H).
    与 5m 表 schema 保持一致。1H 粒度用于补足 OKX 5m 历史窗口较短导致的
    research lookback 缺口。
    """
    __tablename__ = "market_long_short_ratio_1h"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", name="market_long_short_ratio_1h_pkey"),
        Index("idx_brz_ls_ratio_1h_ts", "ts"),
        Index("idx_brz_ls_ratio_1h_sym_ts", "symbol", "ts"),
        {"schema": "bronze"},
    )

    symbol = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)             # 1h bar 起点 (UTC)
    ls_ratio_positions = Column(Numeric(18, 10))
    ls_ratio_accounts = Column(Numeric(18, 10))
    ingest_run_id = Column(UUID(as_uuid=False), nullable=False)
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
# GOVERNANCE Schema — 26 张表
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


class ResearchHoldoutAccessLedgerModel(RdpBase):
    """One-time, fail-closed access ledger for sealed Research Factory holdouts."""

    __tablename__ = "research_holdout_access_ledger"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "holdout_content_fingerprint",
            name="uq_holdout_candidate_fingerprint",
        ),
        Index("ix_holdout_access_status_time", "status", "accessed_at"),
        CheckConstraint(
            "status IN ('access_started', 'evaluated_pass', "
            "'evaluated_fail', 'access_failed')",
            name="ck_holdout_access_status",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0",
            name="ck_holdout_reason_nonempty",
        ),
        CheckConstraint(
            "length(btrim(candidate_id)) > 0 AND length(btrim(actor)) > 0 "
            "AND holdout_content_fingerprint ~ '^rfseg_[0-9a-f]{64}$' "
            "AND git_commit ~ '^[0-9a-f]{40,64}$' "
            "AND (artifact_sha256 IS NULL OR "
            "artifact_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_holdout_identity_shape",
        ),
        CheckConstraint(
            "(status = 'access_started' AND completed_at IS NULL "
            "AND artifact_path IS NULL AND artifact_sha256 IS NULL "
            "AND result_payload IS NULL AND error_message IS NULL) OR "
            "(status IN ('evaluated_pass', 'evaluated_fail') "
            "AND completed_at IS NOT NULL AND artifact_path IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND result_payload IS NOT NULL "
            "AND error_message IS NULL) OR "
            "(status = 'access_failed' AND completed_at IS NOT NULL "
            "AND artifact_path IS NULL AND artifact_sha256 IS NULL "
            "AND result_payload IS NULL AND length(btrim(error_message)) > 0)",
            name="ck_holdout_terminal_shape",
        ),
        {"schema": "governance"},
    )

    access_id = Column(
        UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
    )
    candidate_id = Column(String(160), nullable=False)
    holdout_content_fingerprint = Column(String(80), nullable=False)
    actor = Column(String(128), nullable=False)
    reason = Column(Text, nullable=False)
    git_commit = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'access_started'"))
    artifact_path = Column(Text)
    artifact_sha256 = Column(String(64))
    result_payload = Column(JSONB)
    error_message = Column(Text)
    accessed_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at = Column(DateTime(timezone=True))


class ParameterActivationOperationModel(RdpBase):
    """Execution-owned generation lifecycle; this row alone never activates runtime."""

    __tablename__ = "parameter_activation_operations"
    __table_args__ = (
        UniqueConstraint(
            "scope", "scope_ref", "generation", name="uq_parameter_activation_generation"
        ),
        Index("ix_parameter_activation_state_time", "state", "created_at"),
        Index(
            "uq_parameter_activation_nonterminal_scope",
            "scope",
            "scope_ref",
            unique=True,
            postgresql_where=text(
                "state IN ('pending', 'preparing', 'prepared', 'committing', "
                "'rollback_required', 'rolling_back')"
            ),
        ),
        CheckConstraint(
            "operation_type IN ('apply', 'rollback')",
            name="ck_parameter_activation_operation_type",
        ),
        CheckConstraint(
            "state IN ('pending', 'preparing', 'prepared', 'committing', "
            "'succeeded', 'failed', 'rollback_required', 'rolling_back', "
            "'rolled_back')",
            name="ck_parameter_activation_state",
        ),
        CheckConstraint(
            "cardinality(expected_process_roles) > 0",
            name="ck_parameter_activation_roles_nonempty",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0",
            name="ck_parameter_activation_reason_nonempty",
        ),
        CheckConstraint(
            "length(btrim(scope)) > 0 AND length(btrim(scope_ref)) > 0 "
            "AND length(btrim(generation)) > 0 AND length(btrim(actor)) > 0 "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$' "
            "AND to_parameter_set_id IS NOT NULL AND deadline_at > created_at",
            name="ck_parameter_activation_identity_shape",
        ),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'rolled_back') "
            "AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('succeeded', 'failed', 'rolled_back') "
            "AND terminal_at IS NULL)",
            name="ck_parameter_activation_terminal_shape",
        ),
        {"schema": "governance"},
    )

    operation_id = Column(String(128), primary_key=True)
    operation_type = Column(String(16), nullable=False)
    scope = Column(String(32), nullable=False)
    scope_ref = Column(String(160), nullable=False)
    generation = Column(String(128), nullable=False)
    from_parameter_set_id = Column(String(128))
    to_parameter_set_id = Column(String(128))
    payload_sha256 = Column(String(64), nullable=False)
    state = Column(String(32), nullable=False, server_default=text("'pending'"))
    expected_process_roles = Column(ARRAY(Text), nullable=False)
    actor = Column(String(128), nullable=False)
    reason = Column(Text, nullable=False)
    error_message = Column(Text)
    deadline_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    terminal_at = Column(DateTime(timezone=True))


class DataSourceRegistryModel(RdpBase):
    """不可混淆的数据来源登记。"""

    __tablename__ = "data_source_registry"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_data_source_registry_key"),
        Index("idx_data_source_registry_kind_provider", "source_kind", "provider"),
        CheckConstraint(
            "source_kind IN ('aats_ws_capture','okx_rest','okx_bulk','third_party','derived','proxy')",
            name="chk_data_source_registry_kind",
        ),
        CheckConstraint(
            "truth_tier IN ('authoritative_external','local_observation','derived','proxy','external_unverified')",
            name="chk_data_source_registry_truth_tier",
        ),
        CheckConstraint(
            "(source_kind = 'aats_ws_capture' AND truth_tier = 'local_observation') OR "
            "(source_kind IN ('okx_rest','okx_bulk') AND "
            "truth_tier = 'authoritative_external') OR "
            "(source_kind = 'third_party' AND truth_tier = 'external_unverified') OR "
            "(source_kind = 'derived' AND truth_tier = 'derived') OR "
            "(source_kind = 'proxy' AND truth_tier = 'proxy')",
            name="chk_data_source_registry_kind_truth",
        ),
        {"schema": "meta"},
    )

    source_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    source_key = Column(Text, nullable=False)
    source_kind = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    source_locator = Column(Text, nullable=False)
    schema_version = Column(Text, nullable=False)
    timestamp_semantics = Column(Text, nullable=False)
    truth_tier = Column(Text, nullable=False)
    license_usage_note = Column(Text, nullable=False)
    source_metadata = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ArchivePartitionModel(RdpBase):
    """热数据删除前的不可变归档分区与状态机。"""

    __tablename__ = "archive_partitions"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "dataset_name", "symbol", "coverage_start", "coverage_end",
            name="uq_archive_partition_scope",
        ),
        Index("idx_archive_partition_state", "state", "coverage_end"),
        Index("idx_archive_partition_dataset", "dataset_name", "symbol", "coverage_start"),
        CheckConstraint("coverage_end > coverage_start", name="chk_archive_partition_range"),
        CheckConstraint("row_count >= 0", name="chk_archive_partition_row_count"),
        CheckConstraint(
            "sha256 IS NULL OR "
            "(length(sha256) = 64 AND sha256 = lower(sha256))",
            name="chk_archive_partition_sha",
        ),
        CheckConstraint(
            "state IN ('DISCOVERED','ARCHIVING','VERIFIED','DELETE_ELIGIBLE','DELETED','FAILED')",
            name="chk_archive_partition_state",
        ),
        CheckConstraint(
            "(state IN ('DISCOVERED','ARCHIVING') AND verified_at IS NULL "
            "AND deleted_at IS NULL) OR "
            "(state = 'FAILED' AND error_message IS NOT NULL "
            "AND deleted_at IS NULL) OR "
            "(state IN ('VERIFIED','DELETE_ELIGIBLE') AND sha256 IS NOT NULL "
            "AND row_count > 0 AND verified_at IS NOT NULL "
            "AND deleted_at IS NULL AND error_message IS NULL) OR "
            "(state = 'DELETED' AND sha256 IS NOT NULL AND row_count > 0 "
            "AND verified_at IS NOT NULL AND deleted_at IS NOT NULL "
            "AND error_message IS NULL)",
            name="chk_archive_partition_state_shape",
        ),
        CheckConstraint("storage_format = 'parquet'", name="chk_archive_partition_format"),
        {"schema": "meta"},
    )

    partition_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    source_id = Column(UUID(as_uuid=False), ForeignKey("meta.data_source_registry.source_id"), nullable=False)
    dataset_name = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    coverage_start = Column(DateTime(timezone=True), nullable=False)
    coverage_end = Column(DateTime(timezone=True), nullable=False)
    storage_format = Column(Text, nullable=False, server_default=text("'parquet'"))
    storage_path = Column(Text, nullable=False)
    sha256 = Column(String(64))
    row_count = Column(BigInteger, nullable=False, server_default=text("0"))
    min_event_ts = Column(DateTime(timezone=True))
    max_event_ts = Column(DateTime(timezone=True))
    min_sequence = Column(BigInteger)
    max_sequence = Column(BigInteger)
    gap_manifest = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    manifest_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state = Column(Text, nullable=False, server_default=text("'DISCOVERED'"))
    verified_at = Column(DateTime(timezone=True))
    deleted_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DataGapRecordModel(RdpBase):
    """可审计的数据缺口分类，不允许把未知状态包装成有效零。"""

    __tablename__ = "data_gap_records"
    __table_args__ = (
        UniqueConstraint(
            "dataset_name", "symbol", "channel", "gap_start", "gap_end", "reason_code",
            name="uq_data_gap_scope_reason",
        ),
        Index("idx_data_gap_status", "status", "gap_start"),
        Index("idx_data_gap_dataset", "dataset_name", "symbol", "gap_start"),
        CheckConstraint("gap_end > gap_start", name="chk_data_gap_range"),
        CheckConstraint(
            "classification IN ('deterministic_rebuild','official_backfill','third_party_candidate','prospective_only','cannot_recover')",
            name="chk_data_gap_classification",
        ),
        CheckConstraint(
            "status IN ('OPEN','CLASSIFIED','BACKFILLED','REBUILT','AWAITING_LIVE_COLLECTION','CANNOT_RECOVER','THIRD_PARTY_ONLY')",
            name="chk_data_gap_status",
        ),
        CheckConstraint(
            "status IN ('OPEN','CLASSIFIED') OR "
            "(classification = 'deterministic_rebuild' AND status = 'REBUILT') OR "
            "(classification = 'official_backfill' AND status = 'BACKFILLED') OR "
            "(classification = 'third_party_candidate' AND status = 'THIRD_PARTY_ONLY') OR "
            "(classification = 'prospective_only' AND status = 'AWAITING_LIVE_COLLECTION') OR "
            "(classification = 'cannot_recover' AND status = 'CANNOT_RECOVER')",
            name="chk_data_gap_status_classification",
        ),
        CheckConstraint(
            "(status IN ('BACKFILLED','REBUILT','CANNOT_RECOVER','THIRD_PARTY_ONLY') "
            "AND resolved_at IS NOT NULL) OR "
            "(status NOT IN ('BACKFILLED','REBUILT','CANNOT_RECOVER','THIRD_PARTY_ONLY') "
            "AND resolved_at IS NULL)",
            name="chk_data_gap_resolution_shape",
        ),
        {"schema": "meta"},
    )

    gap_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    source_id = Column(UUID(as_uuid=False), ForeignKey("meta.data_source_registry.source_id"))
    dataset_name = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    channel = Column(Text, nullable=False, server_default=text("''"))
    gap_start = Column(DateTime(timezone=True), nullable=False)
    gap_end = Column(DateTime(timezone=True), nullable=False)
    classification = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'OPEN'"))
    reason_code = Column(Text, nullable=False)
    evidence = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    detected_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    resolved_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DatasetBundleModel(RdpBase):
    """一次研究所用来源集合；组合 bundle 不伪造公共 ingest run。"""

    __tablename__ = "dataset_bundles"
    __table_args__ = (
        UniqueConstraint("bundle_key", name="uq_dataset_bundle_key"),
        UniqueConstraint("fingerprint", name="uq_dataset_bundle_fingerprint"),
        Index("idx_dataset_bundle_mode_status", "eligibility_mode", "status"),
        CheckConstraint("coverage_end > coverage_start", name="chk_dataset_bundle_range"),
        CheckConstraint(
            "length(fingerprint) = 64 AND fingerprint = lower(fingerprint)",
            name="chk_dataset_bundle_fingerprint",
        ),
        CheckConstraint(
            "eligibility_mode IN ('historical_research','live_capture')",
            name="chk_dataset_bundle_mode",
        ),
        CheckConstraint("status IN ('BUILDING','ELIGIBLE','INELIGIBLE','SUPERSEDED')", name="chk_dataset_bundle_status"),
        {"schema": "meta"},
    )

    bundle_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    bundle_key = Column(Text, nullable=False)
    dataset_version = Column(Text, nullable=False)
    purpose = Column(Text, nullable=False)
    eligibility_mode = Column(Text, nullable=False)
    component_sources = Column(JSONB, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    coverage_start = Column(DateTime(timezone=True), nullable=False)
    coverage_end = Column(DateTime(timezone=True), nullable=False)
    status = Column(Text, nullable=False, server_default=text("'BUILDING'"))
    eligibility_report = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DataRebuildRunModel(RdpBase):
    """Silver/Gold/artifact 的确定性重建记录。"""

    __tablename__ = "data_rebuild_runs"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_data_rebuild_operation"),
        Index("idx_data_rebuild_status", "status", "created_at"),
        CheckConstraint("status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="chk_data_rebuild_status"),
        CheckConstraint("rows_read >= 0 AND rows_written >= 0", name="chk_data_rebuild_counts"),
        CheckConstraint(
            "length(input_fingerprint) = 64 "
            "AND input_fingerprint = lower(input_fingerprint) "
            "AND (output_fingerprint IS NULL OR "
            "(length(output_fingerprint) = 64 "
            "AND output_fingerprint = lower(output_fingerprint)))",
            name="chk_data_rebuild_hashes",
        ),
        CheckConstraint(
            "git_commit = lower(git_commit) AND "
            "length(git_commit) IN (40, 64)",
            name="chk_data_rebuild_git_commit",
        ),
        CheckConstraint(
            "(status = 'PLANNED' AND started_at IS NULL AND ended_at IS NULL "
            "AND output_fingerprint IS NULL AND error_message IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND ended_at IS NULL "
            "AND output_fingerprint IS NULL AND error_message IS NULL) OR "
            "(status = 'SUCCEEDED' AND started_at IS NOT NULL "
            "AND ended_at IS NOT NULL AND output_fingerprint IS NOT NULL "
            "AND error_message IS NULL) OR "
            "(status = 'FAILED' AND started_at IS NOT NULL "
            "AND ended_at IS NOT NULL AND output_fingerprint IS NULL "
            "AND error_message IS NOT NULL) OR "
            "(status = 'CANCELLED' AND ended_at IS NOT NULL "
            "AND output_fingerprint IS NULL)",
            name="chk_data_rebuild_state_shape",
        ),
        {"schema": "meta"},
    )

    rebuild_run_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    operation_key = Column(Text, nullable=False)
    bundle_id = Column(UUID(as_uuid=False), ForeignKey("meta.dataset_bundles.bundle_id"), nullable=False)
    transform_version = Column(Text, nullable=False)
    git_commit = Column(String(64), nullable=False)
    rebuild_scope = Column(JSONB, nullable=False)
    input_fingerprint = Column(String(64), nullable=False)
    output_fingerprint = Column(String(64))
    status = Column(Text, nullable=False, server_default=text("'PLANNED'"))
    rows_read = Column(BigInteger, nullable=False, server_default=text("0"))
    rows_written = Column(BigInteger, nullable=False, server_default=text("0"))
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class HistoricalResearchArtifactModel(RdpBase):
    """Versioned source-aware Gold output plus its quality/index evidence."""

    __tablename__ = "historical_research_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "operation_key",
            name="uq_historical_research_artifact_operation",
        ),
        Index(
            "idx_historical_research_artifact_scope",
            "symbol",
            "timeframe",
            "coverage_start",
            "coverage_end",
            "status",
        ),
        Index(
            "idx_historical_research_artifact_primary_bundle",
            "primary_bundle_id",
        ),
        CheckConstraint(
            "artifact_type = 'gold_replay_bars'",
            name="chk_historical_research_artifact_type",
        ),
        CheckConstraint(
            "timeframe IN ('15m','1H')",
            name="chk_historical_research_artifact_timeframe",
        ),
        CheckConstraint(
            "coverage_end > coverage_start",
            name="chk_historical_research_artifact_range",
        ),
        CheckConstraint(
            "jsonb_typeof(input_bundles) = 'array' "
            "AND jsonb_array_length(input_bundles) > 0",
            name="chk_historical_research_artifact_input_shape",
        ),
        CheckConstraint(
            "length(input_fingerprint) = 64 "
            "AND input_fingerprint = lower(input_fingerprint) "
            "AND (output_fingerprint IS NULL OR "
            "(length(output_fingerprint) = 64 "
            "AND output_fingerprint = lower(output_fingerprint)))",
            name="chk_historical_research_artifact_hashes",
        ),
        CheckConstraint(
            "git_commit = lower(git_commit) AND length(git_commit) IN (40, 64)",
            name="chk_historical_research_artifact_git_commit",
        ),
        CheckConstraint(
            "status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name="chk_historical_research_artifact_status",
        ),
        CheckConstraint(
            "row_count >= 0",
            name="chk_historical_research_artifact_row_count",
        ),
        CheckConstraint(
            "(status = 'PLANNED' AND started_at IS NULL AND ended_at IS NULL "
            "AND output_fingerprint IS NULL AND quality_report IS NULL "
            "AND artifact_index IS NULL AND error_message IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND ended_at IS NULL "
            "AND output_fingerprint IS NULL AND quality_report IS NULL "
            "AND artifact_index IS NULL AND error_message IS NULL) OR "
            "(status = 'SUCCEEDED' AND started_at IS NOT NULL "
            "AND ended_at IS NOT NULL AND output_fingerprint IS NOT NULL "
            "AND quality_report IS NOT NULL AND artifact_index IS NOT NULL "
            "AND error_message IS NULL) OR "
            "(status = 'FAILED' AND started_at IS NOT NULL "
            "AND ended_at IS NOT NULL AND output_fingerprint IS NULL "
            "AND quality_report IS NULL AND artifact_index IS NULL "
            "AND error_message IS NOT NULL) OR "
            "(status = 'CANCELLED' AND ended_at IS NOT NULL "
            "AND output_fingerprint IS NULL)",
            name="chk_historical_research_artifact_state_shape",
        ),
        {"schema": "meta"},
    )

    artifact_id = Column(
        UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
    )
    operation_key = Column(Text, nullable=False)
    artifact_type = Column(
        Text, nullable=False, server_default=text("'gold_replay_bars'")
    )
    primary_bundle_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.dataset_bundles.bundle_id"),
        nullable=False,
    )
    symbol = Column(Text, nullable=False)
    timeframe = Column(Text, nullable=False)
    coverage_start = Column(DateTime(timezone=True), nullable=False)
    coverage_end = Column(DateTime(timezone=True), nullable=False)
    input_bundles = Column(JSONB, nullable=False)
    input_fingerprint = Column(String(64), nullable=False)
    transform_version = Column(Text, nullable=False)
    git_commit = Column(String(64), nullable=False)
    status = Column(Text, nullable=False, server_default=text("'PLANNED'"))
    row_count = Column(BigInteger, nullable=False, server_default=text("0"))
    output_fingerprint = Column(String(64))
    quality_report = Column(JSONB)
    artifact_index = Column(JSONB)
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class GoldHistoricalReplayBarModel(RdpBase):
    """Immutable/versioned Gold rows bound to an evidence artifact."""

    __tablename__ = "historical_replay_bars"
    __table_args__ = (
        PrimaryKeyConstraint(
            "artifact_id",
            "symbol",
            "timeframe",
            "ts",
            name="pk_gold_historical_replay_bars",
        ),
        Index(
            "idx_gold_historical_replay_scope",
            "symbol",
            "timeframe",
            "ts",
        ),
        Index(
            "idx_gold_historical_replay_candle_bundle",
            "source_candle_bundle_id",
            "symbol",
            "timeframe",
            "ts",
        ),
        CheckConstraint(
            "timeframe IN ('15m','1H')",
            name="chk_gold_historical_replay_timeframe",
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0 "
            "AND high >= open AND high >= close AND high >= low "
            "AND low <= open AND low <= close",
            name="chk_gold_historical_replay_ohlc",
        ),
        CheckConstraint(
            "(volume IS NULL OR volume >= 0) "
            "AND (quote_volume IS NULL OR quote_volume >= 0)",
            name="chk_gold_historical_replay_volume",
        ),
        CheckConstraint(
            "(source_funding_bundle_id IS NULL AND aligned_funding_rate IS NULL "
            "AND funding_source_ts IS NULL) OR "
            "(source_funding_bundle_id IS NOT NULL "
            "AND (funding_source_ts IS NULL OR funding_source_ts <= ts))",
            name="chk_gold_historical_replay_funding_shape",
        ),
        CheckConstraint(
            "jsonb_typeof(source_lineage) = 'array' "
            "AND jsonb_array_length(source_lineage) > 0",
            name="chk_gold_historical_replay_lineage_shape",
        ),
        CheckConstraint(
            "length(output_fingerprint) = 64 "
            "AND output_fingerprint = lower(output_fingerprint)",
            name="chk_gold_historical_replay_fingerprint",
        ),
        {"schema": "gold"},
    )

    artifact_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.historical_research_artifacts.artifact_id"),
        nullable=False,
    )
    symbol = Column(Text, nullable=False)
    timeframe = Column(Text, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False)
    open = Column(Numeric(20, 10), nullable=False)
    high = Column(Numeric(20, 10), nullable=False)
    low = Column(Numeric(20, 10), nullable=False)
    close = Column(Numeric(20, 10), nullable=False)
    volume = Column(Numeric(28, 10))
    quote_volume = Column(Numeric(28, 10))
    is_closed = Column(Boolean, nullable=False)
    aligned_funding_rate = Column(Numeric(18, 12))
    funding_source_ts = Column(DateTime(timezone=True))
    source_candle_bundle_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.dataset_bundles.bundle_id"),
        nullable=False,
    )
    source_funding_bundle_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.dataset_bundles.bundle_id"),
    )
    source_lineage = Column(JSONB, nullable=False)
    transform_version = Column(Text, nullable=False)
    output_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class HistoricalCampaignRunModel(RdpBase):
    """Capacity-gated multi-day official-history campaign checkpoint."""

    __tablename__ = "historical_campaign_runs"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_historical_campaign_operation"),
        Index(
            "idx_historical_campaign_scope",
            "symbol",
            "coverage_start",
            "coverage_end",
            "status",
        ),
        CheckConstraint(
            "coverage_end > coverage_start",
            name="chk_historical_campaign_range",
        ),
        CheckConstraint(
            "requested_days > 0",
            name="chk_historical_campaign_days",
        ),
        CheckConstraint(
            "status IN ('PLANNED','BLOCKED','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name="chk_historical_campaign_status",
        ),
        CheckConstraint(
            "jsonb_typeof(capacity_report) = 'object' "
            "AND jsonb_typeof(manifest) = 'object' "
            "AND jsonb_typeof(checkpoint) = 'object'",
            name="chk_historical_campaign_json",
        ),
        CheckConstraint(
            "(status = 'BLOCKED' "
            "AND capacity_report @> '{\"approved\": false}'::jsonb) OR "
            "(status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED') "
            "AND capacity_report @> '{\"approved\": true}'::jsonb) OR "
            "status = 'CANCELLED'",
            name="chk_historical_campaign_capacity_status",
        ),
        CheckConstraint(
            "(status IN ('PLANNED','BLOCKED') AND started_at IS NULL "
            "AND ended_at IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL "
            "AND ended_at IS NULL AND error_message IS NULL) OR "
            "(status = 'SUCCEEDED' AND started_at IS NOT NULL "
            "AND ended_at IS NOT NULL AND error_message IS NULL) OR "
            "(status = 'FAILED' AND started_at IS NOT NULL "
            "AND ended_at IS NOT NULL AND error_message IS NOT NULL) OR "
            "(status = 'CANCELLED' AND ended_at IS NOT NULL)",
            name="chk_historical_campaign_state_shape",
        ),
        {"schema": "meta"},
    )

    campaign_id = Column(
        UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
    )
    operation_key = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    coverage_start = Column(DateTime(timezone=True), nullable=False)
    coverage_end = Column(DateTime(timezone=True), nullable=False)
    requested_days = Column(Integer, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'PLANNED'"))
    capacity_report = Column(JSONB, nullable=False)
    manifest = Column(JSONB, nullable=False)
    checkpoint = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class CollectorContinuityEventModel(RdpBase):
    """按连接代次持久化采集器连续性事件。"""

    __tablename__ = "collector_continuity_events"
    __table_args__ = (
        UniqueConstraint(
            "collector", "channel", "symbol", "ingest_run_id",
            "connection_generation", "event_type", "event_ts", "event_key",
            name="uq_collector_continuity_event",
        ),
        Index("idx_collector_continuity_window", "collector", "channel", "symbol", "event_ts"),
        Index("idx_collector_continuity_generation", "collector", "connection_generation"),
        CheckConstraint("connection_generation >= 0", name="chk_collector_generation"),
        CheckConstraint(
            "event_type IN ('CONNECT','DISCONNECT','RECONNECT','MESSAGE','FLUSH','DROP','SHUTDOWN','CLOCK_SKEW')",
            name="chk_collector_continuity_type",
        ),
        {"schema": "meta"},
    )

    continuity_event_id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    collector = Column(Text, nullable=False)
    channel = Column(Text, nullable=False)
    symbol = Column(Text, nullable=False)
    connection_generation = Column(Integer, nullable=False)
    event_type = Column(Text, nullable=False)
    event_ts = Column(DateTime(timezone=True), nullable=False)
    event_key = Column(Text, nullable=False, server_default=text("''"))
    exchange_event_ts = Column(DateTime(timezone=True))
    local_received_ts = Column(DateTime(timezone=True))
    sample_ts = Column(DateTime(timezone=True))
    payload_sequence = Column(BigInteger)
    ingest_run_id = Column(
        UUID(as_uuid=False),
        ForeignKey("meta.ingest_runs.ingest_run_id"),
        nullable=False,
    )
    details = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ParameterRuntimeAckModel(RdpBase):
    """Per-process prepare/commit/readback/rollback acknowledgement."""

    __tablename__ = "parameter_runtime_acks"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "process_role", "phase", name="uq_parameter_runtime_ack"
        ),
        Index("ix_parameter_runtime_ack_operation", "operation_id", "ack_at"),
        CheckConstraint(
            "phase IN ('prepare', 'commit', 'readback', 'rollback')",
            name="ck_parameter_runtime_ack_phase",
        ),
        CheckConstraint(
            "ack_status IN ('accepted', 'rejected', 'mismatch', 'timeout')",
            name="ck_parameter_runtime_ack_status",
        ),
        CheckConstraint(
            "length(btrim(process_role)) > 0 "
            "AND length(btrim(generation)) > 0 "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_parameter_runtime_ack_identity_shape",
        ),
        {"schema": "governance"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    operation_id = Column(
        String(128),
        ForeignKey(
            "governance.parameter_activation_operations.operation_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    process_role = Column(String(64), nullable=False)
    phase = Column(String(16), nullable=False)
    generation = Column(String(128), nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    ack_status = Column(String(16), nullable=False)
    observed_parameter_set_id = Column(String(128))
    details = Column(JSONB)
    error_message = Column(Text)
    ack_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


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


class RdpRunModel(RdpBase):
    """一次逻辑 RDP 运行；自动重试通过 attempt 复用同一 run_id。"""

    __tablename__ = "rdp_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_rdp_run_id"),
        UniqueConstraint("idempotency_key", name="uq_rdp_run_idempotency"),
        Index("ix_rdp_runs_status_created", "status", "created_at"),
        Index("ix_rdp_runs_workflow_created", "workflow", "created_at"),
        ForeignKeyConstraint(
            ["source_run_id"],
            ["governance.rdp_runs.run_id"],
            name="fk_rdp_run_source",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued','running','cancellation_requested','succeeded',"
            "'succeeded_with_warnings','partially_succeeded','failed','cancelled')",
            name="chk_rdp_run_status",
        ),
        CheckConstraint(
            "research_outcome IN ('unknown','eligible','not_eligible','inconclusive',"
            "'blocked_by_data','blocked_by_attribution','blocked_by_execution')",
            name="chk_rdp_run_outcome",
        ),
        CheckConstraint(
            "trigger_kind IN ('manual','schedule','auto_retry','recovery')",
            name="chk_rdp_run_trigger",
        ),
        CheckConstraint(
            "completed_steps >= 0 AND total_steps >= 0 AND completed_steps <= total_steps",
            name="chk_rdp_run_step_counts",
        ),
        {"schema": "governance"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(128), nullable=False, unique=True)
    workflow = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'queued'"))
    research_outcome = Column(String(64), nullable=False, server_default=text("'unknown'"))
    trigger_kind = Column(String(32), nullable=False, server_default=text("'manual'"))
    requested_by = Column(String(128), nullable=False, server_default=text("'operator'"))
    idempotency_key = Column(String(160), unique=True)
    source_run_id = Column(String(128))
    eligible_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    heartbeat_at = Column(DateTime(timezone=True))
    current_step_key = Column(String(128))
    completed_steps = Column(Integer, nullable=False, server_default=text("0"))
    total_steps = Column(Integer, nullable=False, server_default=text("0"))
    cancel_requested_at = Column(DateTime(timezone=True))
    error_code = Column(String(128))
    error_summary = Column(Text)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RdpRunStepModel(RdpBase):
    __tablename__ = "rdp_run_steps"
    __table_args__ = (
        UniqueConstraint("step_run_id", name="uq_rdp_run_step_id"),
        UniqueConstraint("run_id", "attempt_no", "step_key", name="uq_rdp_run_attempt_step"),
        Index("ix_rdp_run_steps_run_order", "run_id", "attempt_no", "step_order"),
        Index("ix_rdp_run_steps_status", "status", "updated_at"),
        ForeignKeyConstraint(
            ["run_id"],
            ["governance.rdp_runs.run_id"],
            name="fk_rdp_run_step_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("attempt_no >= 1", name="chk_rdp_run_step_attempt"),
        CheckConstraint("step_order >= 0", name="chk_rdp_run_step_order"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped','cancelled')",
            name="chk_rdp_run_step_status",
        ),
        {"schema": "governance"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    step_run_id = Column(String(160), nullable=False, unique=True)
    run_id = Column(String(128), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    step_key = Column(String(128), nullable=False)
    step_order = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'pending'"))
    allow_failure = Column(Boolean, nullable=False, server_default=text("false"))
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    exit_code = Column(Integer)
    error_code = Column(String(128))
    error_summary = Column(Text)
    log_ref = Column(Text)
    artifact_refs = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RdpRunEventModel(RdpBase):
    __tablename__ = "rdp_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_rdp_run_event_sequence"),
        Index("ix_rdp_run_events_run_sequence", "run_id", "sequence_no"),
        Index("ix_rdp_run_events_occurred", "occurred_at"),
        ForeignKeyConstraint(
            ["run_id"],
            ["governance.rdp_runs.run_id"],
            name="fk_rdp_run_event_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("attempt_no IS NULL OR attempt_no >= 1", name="chk_rdp_run_event_attempt"),
        {"schema": "governance"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(128), nullable=False)
    sequence_no = Column(BigInteger, nullable=False)
    attempt_no = Column(Integer)
    step_key = Column(String(128))
    event_type = Column(String(96), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RdpTaskQueueModel(RdpBase):
    __tablename__ = "rdp_task_queue"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_rdp_task_id"),
        Index("ix_rdp_task_queue_status", "status", "created_at"),
        Index("ix_rdp_task_run_attempt", "run_id", "attempt_no"),
        Index(
            "ix_rdp_task_eligible_priority",
            "status",
            "earliest_start_at",
            "priority_class",
            "created_at",
        ),
        Index(
            "ix_rdp_task_one_active_per_workflow",
            "workflow",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        CheckConstraint(
            "status IN ('pending','running','done','failed','cancelled')",
            name="chk_rdp_task_status",
        ),
        CheckConstraint("attempt_no >= 1", name="chk_rdp_task_attempt_no"),
        CheckConstraint(
            "trigger_kind IN ('manual','schedule','auto_retry','recovery')",
            name="chk_rdp_task_trigger_kind",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            ["governance.rdp_runs.run_id"],
            name="fk_rdp_task_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parent_task_id"],
            ["governance.rdp_task_queue.task_id"],
            name="fk_rdp_task_parent",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(128), nullable=False, unique=True)
    run_id = Column(String(128), nullable=False)
    attempt_no = Column(Integer, nullable=False, server_default=text("1"))
    parent_task_id = Column(String(128))
    workflow = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'pending'"))
    requested_by = Column(String(128), nullable=False, server_default=text("'operator'"))
    requested_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    # R3 Bug 6 retry 延迟机制 (2026-04-19):
    # daemon claim 时要求 earliest_start_at <= now()，让 auto_retry task 能延迟
    # 15min 后才被领取。scheduler 正常入队时默认 = created_at（立即可领）。
    # 现有数据 server_default='now()' 自动兼容（升级后旧 row 立刻 claimable）。
    earliest_start_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    trigger_kind = Column(String(32), nullable=False, server_default=text("'manual'"))
    priority_class = Column(String(32), nullable=False, server_default=text("'normal'"))
    heartbeat_at = Column(DateTime(timezone=True))
    cancel_requested_at = Column(DateTime(timezone=True))
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


class ReleaseEffectivenessActionProofModel(RdpBase):
    """Application insert-once proof ledger for terminal action outcomes.

    ``release_effectiveness.payload`` is an operator-facing projection.  A
    caller-controlled JSON flag must never authorize capital flow, so terminal
    resolution additionally requires one row written by the combo-locked DB
    transition after canonical capital/decision truth was re-derived.  The
    schema enforces FK/UNIQUE/shape constraints; database roles must separately
    prevent ad-hoc UPDATE/DELETE because no immutable trigger exists.
    """

    __tablename__ = "release_effectiveness_action_proofs"
    __table_args__ = (
        UniqueConstraint("release_id", name="uq_release_eff_action_proof_release"),
        UniqueConstraint("attempt_id", name="uq_release_eff_action_proof_attempt"),
        ForeignKeyConstraint(
            ["release_id"],
            ["governance.parameter_releases.release_id"],
            name="fk_release_eff_action_proof_release",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "outcome IN ('enforced', 'cancelled')",
            name="ck_release_eff_action_proof_outcome",
        ),
        CheckConstraint(
            "proof_kind IN ('rollback', 'active_parameter_changed', 'soft_pause')",
            name="ck_release_eff_action_proof_kind",
        ),
        CheckConstraint(
            "(outcome = 'enforced' AND proof_kind = 'rollback' "
            " AND operation_id IS NOT NULL AND target_parameter_set_id IS NOT NULL"
            " AND observed_active_parameter_set_id IS NULL AND decision_status IS NULL)"
            " OR (outcome = 'cancelled' AND proof_kind = 'active_parameter_changed'"
            " AND operation_id IS NULL AND target_parameter_set_id IS NULL"
            " AND observed_active_parameter_set_id IS NOT NULL"
            " AND decision_status IS NULL)"
            " OR (outcome = 'cancelled' AND proof_kind = 'soft_pause'"
            " AND operation_id IS NULL AND target_parameter_set_id IS NULL"
            " AND observed_active_parameter_set_id IS NULL"
            " AND decision_status = 'pause')",
            name="ck_release_eff_action_proof_shape",
        ),
        Index("ix_release_eff_action_proof_created", "created_at"),
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    release_id = Column(String(128), nullable=False, unique=True)
    attempt_id = Column(String(128), nullable=False, unique=True)
    outcome = Column(String(32), nullable=False)
    proof_kind = Column(String(64), nullable=False)
    started_at_utc = Column(String(40), nullable=False)
    finished_at_utc = Column(String(40), nullable=False)
    operation_id = Column(String(128))
    target_parameter_set_id = Column(String(128))
    observed_active_parameter_set_id = Column(String(128))
    decision_status = Column(String(32))
    fact_observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


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
    """创建 RDP 的全部 7 个 PostgreSQL schema + 当前 102 张表。

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
