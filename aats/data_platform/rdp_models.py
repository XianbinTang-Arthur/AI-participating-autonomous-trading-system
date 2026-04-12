"""RDP (Research Data Platform) SQLAlchemy ORM 模型。

替代 migrations/research/*.sql，通过 RdpBase.metadata.create_all() 自动建表。
47 张表分布在 7 个 PostgreSQL schema：meta / staging / bronze / silver /
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
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    parameter_set_id = Column(String(128), nullable=False, unique=True)
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
        {"schema": "governance"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = Column(String(128), nullable=False, unique=True)
    family = Column(String(64), nullable=False)
    symbol = Column(String(32), nullable=False, server_default=text("'BTC-USDT-SWAP'"))
    timeframe = Column(String(16), nullable=False)
    recommendation_type = Column(String(32), nullable=False)
    target_parameter_set_id = Column(String(128))
    confidence = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False)
    evidence_bundle_ref = Column(String(128))
    status = Column(String(32), nullable=False, server_default=text("'draft'"))
    approved_by = Column(String(128))
    approved_at = Column(DateTime(timezone=True))
    approval_notes = Column(Text)
    rejected_by = Column(String(128))
    rejected_at = Column(DateTime(timezone=True))
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
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    exit_code = Column(Integer)
    error_message = Column(Text)
    log_tail = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# =====================================================================
# Schema 创建入口
# =====================================================================

def create_rdp_schema(engine: object) -> None:
    """创建 RDP 的全部 7 个 PostgreSQL schema + 47 张表。

    替代 migrations/research/*.sql 迁移文件。幂等——已存在的 schema/表不会
    被破坏（CREATE SCHEMA IF NOT EXISTS + create_all 的 checkfirst=True）。
    """
    from sqlalchemy import text as _text

    with engine.begin() as conn:  # type: ignore[union-attr]
        for schema in _RDP_SCHEMAS:
            conn.execute(_text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))

    RdpBase.metadata.create_all(engine)  # type: ignore[arg-type]
