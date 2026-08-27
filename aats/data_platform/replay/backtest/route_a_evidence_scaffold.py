"""Route A phase 0 evidence bundle scaffold helper.

给**已经存在**的 Route A candidate 打包最小 evidence bundle 骨架：

1. 校验 scorecard JSON / observation-window JSON 的必备顶层字段
2. 创建 ``<output_root>/<proposal_id>/`` 目录 (已存在即拒绝覆盖)
3. 复制两份 input JSON 到 bundle 目录
4. 写 ``manifest.json`` (proposal meta + source provenance + sha256)
5. 写 ``proposal.md`` (模板预填 metadata + artifact 引用)

严格边界 (和 SoW 对齐)
----------------------
* **不**输出 verdict / go-no-go / archive 判定
* **不**触 live path / configs / deploy
* **不**读 ``.env.*`` 或任何凭证
* 纯本地 FS 操作，无 DB / 网络副作用
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aats.data_platform.replay.backtest.evidence_scorecard import (
    SCORECARD_ARTIFACT_KIND,
    SCORECARD_COST_ADJUSTED_KEYS,
    SCORECARD_COST_BUCKET_KEYS,
    SCORECARD_FILL_MODEL_VERSION,
    SCORECARD_INSTRUMENT_CONTRACT_KEYS,
    SCORECARD_META_KEYS,
    SCORECARD_OOS_KEYS,
    SCORECARD_REGIME_BUCKET_KEYS,
    SCORECARD_REGIME_KEYS,
    SCORECARD_RESOLVED_COST_KEYS,
    SCORECARD_RESOLVED_PARAMETER_KEYS,
    SCORECARD_SCHEMA_VERSION,
    SCORECARD_SENSITIVITY_BUCKET_KEYS,
    SCORECARD_SENSITIVITY_KEYS,
    SCORECARD_SLICE_KEYS,
    SCORECARD_TOP_LEVEL_KEYS,
    SCORECARD_VOL_KEYS,
)
from aats.data_platform.replay.backtest.equity_builder import (
    REPLAY_RISK_METRIC_POLICY_ID,
)
from aats.data_platform.replay.backtest.numeric import validate_finite_numbers
from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
from aats.domain.instrument_contract import (
    INSTRUMENT_ARITHMETIC_POLICY_ID,
    InstrumentContract,
    InstrumentContractError,
)


SCORECARD_REQUIRED_KEYS = SCORECARD_TOP_LEVEL_KEYS
OBSERVATION_WINDOW_ARTIFACT_KIND = "route_a_observation_window_summary"
OBSERVATION_WINDOW_SCHEMA_VERSION = "route-a-observation-window/v1"
OBSERVATION_WINDOW_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "artifact_kind",
        "artifact_schema_version",
        "generated_at",
        "window_start",
        "window_target",
        "overall",
        "exit_code",
        "warn_count",
        "fail_count",
        "checks",
    }
)
BUNDLE_MANIFEST_ARTIFACT_KIND = "route_a_evidence_bundle_manifest"
BUNDLE_MANIFEST_SCHEMA_VERSION = "route-a-evidence-bundle/v2"

DEFAULT_OUTPUT_ROOT: Path = Path("docs/research/route_a_phase0")

_COPIED_SCORECARD_NAME = "scorecard.json"
_COPIED_OBSERVATION_NAME = "observation_window_summary.json"
_MANIFEST_NAME = "manifest.json"
_PROPOSAL_MD_NAME = "proposal.md"
_CONTRACT_REQUIRED_KEYS = SCORECARD_INSTRUMENT_CONTRACT_KEYS
_SCORECARD_META_KEYS = SCORECARD_META_KEYS
_RESOLVED_PARAMETER_KEYS = SCORECARD_RESOLVED_PARAMETER_KEYS
_RESOLVED_COST_CONFIG_KEYS = SCORECARD_RESOLVED_COST_KEYS
_EXPECTED_MARKET_DATA_GRANULARITY = "ohlcv"
_EXPECTED_EXECUTION_REALISM_LIMITATIONS = [
    "no_l2_depth",
    "no_spread_or_queue_position",
    "no_market_impact_calibration",
    "fixed_slippage_bps",
    "volume_participation_proxy_only",
]
_BUILTIN_ADAPTER_CONTRACTS = {
    "independent": (
        "aats.data_platform.replay.adapters.independent_adapter."
        "IndependentReplayAdapter",
        "independent-replay/v2",
    ),
    "directional": (
        "aats.data_platform.replay.adapters.directional_adapter."
        "DirectionalReplayAdapter",
        "directional-replay/v2",
    ),
}
_TIMEFRAME_MS = {
    "m": 60 * 1000,
    "h": 60 * 60 * 1000,
    "d": 24 * 60 * 60 * 1000,
}
_MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000
_METRIC_REL_TOL = 1e-12
_METRIC_ABS_TOL = 1e-9
_FORBIDDEN_DECISION_TOKENS = frozenset(
    {
        "verdict",
        "go",
        "nogo",
        "pass",
        "fail",
        "verified",
        "capitaleligible",
        "productionready",
        "tradingready",
        "liveready",
        "promotionready",
        "deployable",
        "approved",
        "approval",
        "eligible",
        "archive",
        "decision",
        "recommendation",
        "status",
        "isapproved",
        "isverified",
        "isproductionready",
        "istradingready",
        "isliveready",
        "approvalstatus",
        "promotionstatus",
        "readinessstatus",
        "gatedecision",
        "gateverdict",
        "capitaleligibility",
    }
)


@dataclass(frozen=True)
class ScaffoldInputs:
    """Inputs accepted by :func:`create_scaffold`.

    ``proposer`` 可选，未提供时 proposal.md 中写 ``<TBD>``。
    ``output_root`` 缺省 ``docs/research/route_a_phase0``。
    """

    proposal_id: str
    feature: str
    horizon: str
    scorecard_json: Path
    observation_window_json: Path
    proposer: str | None = None
    output_root: Path = DEFAULT_OUTPUT_ROOT


@dataclass(frozen=True)
class ScaffoldResult:
    """Absolute paths of the bundle artifacts written by :func:`create_scaffold`."""

    proposal_dir: Path
    manifest_path: Path
    scorecard_path: Path
    observation_window_summary_path: Path
    proposal_md_path: Path


class ScaffoldError(Exception):
    """Raised when scaffold preconditions are violated (missing input / bad JSON /
    output dir collision).  Distinct type so the CLI can map it to a clean
    ``SystemExit`` message without swallowing unrelated exceptions.
    """


@dataclass(frozen=True)
class _ValidatedScorecardSlice:
    """One exact scorecard slice plus its independently derived point count."""

    payload: dict[str, Any]
    start: datetime | None
    end: datetime | None
    point_count: int


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_and_validate_json(
    path: Path,
    required_keys: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise ScaffoldError(f"{label} 输入文件不存在: {path}")
    try:
        # Tolerate UTF-8 BOM: PowerShell's default UTF-8 output prepends one,
        # and the stdlib ``utf-8`` codec refuses it.  ``utf-8-sig`` strips the
        # BOM if present and is a no-op otherwise.
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(
            raw,
            parse_constant=lambda token: (_raise_non_finite_json(token)),
        )
    except json.JSONDecodeError as exc:
        raise ScaffoldError(
            f"{label} JSON 解析失败: {path} ({exc.msg} @ line {exc.lineno})"
        ) from exc
    except ValueError as exc:
        raise ScaffoldError(f"{label} JSON 含非有限数值: {path}") from exc
    try:
        # ``parse_constant`` only catches the non-standard NaN/Infinity tokens.
        # A standards-shaped exponent such as ``1e999`` is decoded as ``inf``
        # by CPython and must be rejected recursively as well.
        validate_finite_numbers(data, reason=f"{label}_json_non_finite")
    except ValueError as exc:
        raise ScaffoldError(f"{label} JSON 含非有限数值: {path}") from exc
    if not isinstance(data, dict):
        raise ScaffoldError(f"{label} 顶层必须是 JSON 对象: {path}")
    missing = sorted(required_keys - data.keys())
    if missing:
        raise ScaffoldError(
            f"{label} 缺少必需顶层字段: {missing} (path={path})"
        )
    unknown = sorted(data.keys() - required_keys)
    if unknown:
        raise ScaffoldError(
            f"{label} 含未知顶层字段: {unknown} (path={path})"
        )
    return data


def _raise_non_finite_json(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _require_exact_mapping(
    value: Any,
    *,
    keys: frozenset[str],
    label: str,
    path: Path,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ScaffoldError(f"scorecard.{label} schema 无效: {path}")
    return value


def _require_metric_number(
    value: Any,
    *,
    label: str,
    path: Path,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScaffoldError(f"scorecard.{label} 必须是有限数值: {path}")
    resolved = float(value)
    if minimum is not None and resolved < minimum:
        raise ScaffoldError(f"scorecard.{label} 超出允许范围: {path}")
    if maximum is not None and resolved > maximum:
        raise ScaffoldError(f"scorecard.{label} 超出允许范围: {path}")
    return resolved


def _require_metric_close(
    actual: Any,
    expected: float,
    *,
    label: str,
    path: Path,
) -> None:
    resolved = _require_metric_number(actual, label=label, path=path)
    if not math.isclose(
        resolved,
        expected,
        rel_tol=_METRIC_REL_TOL,
        abs_tol=_METRIC_ABS_TOL,
    ):
        raise ScaffoldError(f"scorecard.{label} 计算口径不一致: {path}")


def _parse_scorecard_timeframe_ms(value: Any, *, path: Path) -> int:
    if not isinstance(value, str):
        raise ScaffoldError(f"scorecard.meta.timeframe 无效: {path}")
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", value)
    if match is None:
        raise ScaffoldError(f"scorecard.meta.timeframe 无效: {path}")
    return int(match.group(1)) * _TIMEFRAME_MS[match.group(2)]


def _timedelta_microseconds(start: datetime, end: datetime) -> int:
    delta = end - start
    return (
        delta.days * 24 * 60 * 60 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _parse_utc_iso_or_none(value: Any, *, label: str, path: Path) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScaffoldError(f"scorecard.{label} 时间戳无效: {path}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScaffoldError(f"scorecard.{label} 时间戳无效: {path}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ScaffoldError(f"scorecard.{label} 必须是 UTC: {path}")
    return parsed


def _validate_scorecard_slice(
    value: Any,
    *,
    label: str,
    path: Path,
    timeframe_ms: int,
    meta_start: datetime,
    meta_end: datetime,
    include_prior_baseline: bool = False,
) -> _ValidatedScorecardSlice:
    bucket = _require_exact_mapping(
        value,
        keys=SCORECARD_SLICE_KEYS,
        label=label,
        path=path,
    )
    start = _parse_utc_iso_or_none(bucket["start"], label=f"{label}.start", path=path)
    end = _parse_utc_iso_or_none(bucket["end"], label=f"{label}.end", path=path)
    if (start is None) != (end is None) or (
        start is not None and end is not None and start > end
    ):
        raise ScaffoldError(f"scorecard.{label} 时间范围无效: {path}")
    for key in ("ir", "ir_annualized", "sharpe_ratio"):
        _require_metric_number(bucket[key], label=f"{label}.{key}", path=path)
    _require_metric_number(
        bucket["hit_rate"],
        label=f"{label}.hit_rate",
        path=path,
        minimum=0.0,
        maximum=1.0,
    )
    _require_metric_number(
        bucket["max_drawdown_bps"],
        label=f"{label}.max_drawdown_bps",
        path=path,
        minimum=0.0,
    )
    for key in ("fills", "sample_n"):
        if type(bucket[key]) is not int or bucket[key] < 0:
            raise ScaffoldError(f"scorecard.{label}.{key} 无效: {path}")
    if start is None and any(
        bucket[key] != 0
        for key in (
            "ir",
            "ir_annualized",
            "sharpe_ratio",
            "hit_rate",
            "fills",
            "sample_n",
            "max_drawdown_bps",
        )
    ):
        raise ScaffoldError(f"scorecard.{label} 空切片必须为零值: {path}")
    if start is None or end is None:
        return _ValidatedScorecardSlice(bucket, None, None, 0)

    if not (meta_start < start <= end <= meta_end):
        raise ScaffoldError(f"scorecard.{label} 超出 replay window: {path}")
    interval_us = _timedelta_microseconds(start, end)
    timeframe_us = timeframe_ms * 1000
    if interval_us % timeframe_us != 0:
        raise ScaffoldError(f"scorecard.{label} 未按 timeframe 对齐: {path}")
    point_count = interval_us // timeframe_us + 1
    expected_sample_n = (
        point_count if include_prior_baseline else max(point_count - 1, 0)
    )
    if bucket["sample_n"] != expected_sample_n:
        raise ScaffoldError(f"scorecard.{label}.sample_n 与时间范围不一致: {path}")
    if bucket["fills"] > point_count:
        raise ScaffoldError(f"scorecard.{label}.fills 超过可归因点数: {path}")

    if bucket["sample_n"] < 2:
        for key in ("ir", "ir_annualized", "sharpe_ratio"):
            if bucket[key] != 0:
                raise ScaffoldError(
                    f"scorecard.{label} IR 样本不足时必须为零: {path}"
                )
    if bucket["sample_n"] == 0:
        for key in ("hit_rate", "max_drawdown_bps"):
            if bucket[key] != 0:
                raise ScaffoldError(
                    f"scorecard.{label} 单点切片指标必须为零: {path}"
                )
    else:
        expected_annualized = float(bucket["ir"]) * math.sqrt(
            _MS_PER_YEAR / timeframe_ms
        )
        _require_metric_close(
            bucket["ir_annualized"],
            expected_annualized,
            label=f"{label}.ir_annualized",
            path=path,
        )
        _require_metric_close(
            bucket["sharpe_ratio"],
            expected_annualized,
            label=f"{label}.sharpe_ratio",
            path=path,
        )
    return _ValidatedScorecardSlice(bucket, start, end, point_count)


def _validate_cost_bucket(
    value: Any,
    *,
    label: str,
    path: Path,
    fill_count: int,
) -> dict[str, float]:
    bucket = _require_exact_mapping(
        value,
        keys=SCORECARD_COST_BUCKET_KEYS,
        label=label,
        path=path,
    )
    resolved = {
        key: _require_metric_number(
            bucket[key],
            label=f"{label}.{key}",
            path=path,
        )
        for key in SCORECARD_COST_BUCKET_KEYS
    }
    if resolved["slip_bps"] < 0:
        raise ScaffoldError(f"scorecard.{label}.slip_bps 不得为负: {path}")
    if fill_count == 0:
        for key in (
            "realized_edge_bps",
            "fee_bps",
            "exec_buffer_bps",
            "net_edge_bps",
        ):
            if resolved[key] != 0:
                raise ScaffoldError(
                    f"scorecard.{label} 空成本桶必须为零值: {path}"
                )
    else:
        _require_metric_close(
            resolved["net_edge_bps"],
            resolved["realized_edge_bps"]
            - resolved["fee_bps"]
            - resolved["slip_bps"],
            label=f"{label}.net_edge_bps",
            path=path,
        )
    return resolved


def _validate_cost_sensitivity(
    bucket: dict[str, float],
    sensitivity: Any,
    *,
    label: str,
    path: Path,
    fill_count: int,
) -> None:
    resolved = _require_exact_mapping(
        sensitivity,
        keys=SCORECARD_SENSITIVITY_BUCKET_KEYS,
        label=label,
        path=path,
    )
    for key in SCORECARD_SENSITIVITY_BUCKET_KEYS:
        _require_metric_number(resolved[key], label=f"{label}.{key}", path=path)
    if fill_count == 0:
        expected_fee_shock = 0.0
        expected_slip_shock = 0.0
    else:
        realized = bucket["realized_edge_bps"]
        fee = bucket["fee_bps"]
        slip = bucket["slip_bps"]
        execution_buffer = bucket["exec_buffer_bps"]
        expected_fee_shock = (
            realized - (fee + abs(fee) * 0.2) - slip - execution_buffer
        )
        expected_slip_shock = (
            realized - fee - (slip + 0.5) - execution_buffer
        )
    _require_metric_close(
        resolved["net_edge_fee_up_20pct_bps"],
        expected_fee_shock,
        label=f"{label}.net_edge_fee_up_20pct_bps",
        path=path,
    )
    _require_metric_close(
        resolved["net_edge_slip_plus_0_5bps_bps"],
        expected_slip_shock,
        label=f"{label}.net_edge_slip_plus_0_5bps_bps",
        path=path,
    )


def _validate_nested_scorecard_contract(
    data: dict[str, Any],
    *,
    path: Path,
    meta_start: datetime,
    meta_end: datetime,
    timeframe_ms: int,
    total_bars: int,
    total_fills: int,
) -> None:
    oos = _require_exact_mapping(
        data.get("oos"),
        keys=SCORECARD_OOS_KEYS,
        label="oos",
        path=path,
    )
    if oos["split_method"] not in {"explicit", "time_midpoint"}:
        raise ScaffoldError(f"scorecard.oos.split_method 无效: {path}")
    split_ts = _parse_utc_iso_or_none(
        oos["split_ts"],
        label="oos.split_ts",
        path=path,
    )
    if oos["split_method"] == "explicit" and split_ts is None:
        raise ScaffoldError(f"scorecard.oos.split_ts 缺失: {path}")
    train = _validate_scorecard_slice(
        oos["train"],
        label="oos.train",
        path=path,
        timeframe_ms=timeframe_ms,
        meta_start=meta_start,
        meta_end=meta_end,
    )
    test = _validate_scorecard_slice(
        oos["test"],
        label="oos.test",
        path=path,
        timeframe_ms=timeframe_ms,
        meta_start=meta_start,
        meta_end=meta_end,
        include_prior_baseline=train.point_count > 0,
    )
    if train.point_count == 0 or test.point_count == 0:
        raise ScaffoldError(f"scorecard.oos 必须包含非空 train/test: {path}")
    if train.point_count + test.point_count != total_bars:
        raise ScaffoldError(f"scorecard.oos 样本与 meta.total_bars 不一致: {path}")
    if (
        train.payload["sample_n"] + test.payload["sample_n"]
        != total_bars - 1
    ):
        raise ScaffoldError(f"scorecard.oos return 分区不闭合: {path}")
    if (
        train.start is None
        or train.end is None
        or test.start is None
        or test.end is None
        or train.end >= test.start
        or _timedelta_microseconds(train.end, test.start) != timeframe_ms * 1000
    ):
        raise ScaffoldError(f"scorecard.oos train/test 时间分区不闭合: {path}")
    if split_ts is None or not (train.end < split_ts <= test.start):
        raise ScaffoldError(f"scorecard.oos.split_ts 与 train/test 不一致: {path}")
    if oos["split_method"] == "time_midpoint":
        expected_split_us = _timedelta_microseconds(train.start, test.end) // 2
        actual_split_us = _timedelta_microseconds(train.start, split_ts)
        if actual_split_us != expected_split_us:
            raise ScaffoldError(f"scorecard.oos time_midpoint 口径不一致: {path}")

    timeframe_us = timeframe_ms * 1000
    cross = data.get("cross_window")
    if not isinstance(cross, list) or len(cross) < 3:
        raise ScaffoldError(f"scorecard.cross_window 至少需要 3 个切片: {path}")
    cross_buckets = [
        _validate_scorecard_slice(
            bucket,
            label=f"cross_window[{index}]",
            path=path,
            timeframe_ms=timeframe_ms,
            meta_start=meta_start,
            meta_end=meta_end,
            include_prior_baseline=True,
        )
        for index, bucket in enumerate(cross)
    ]
    if any(bucket.point_count == 0 for bucket in cross_buckets):
        raise ScaffoldError(f"scorecard.cross_window 不得包含空切片: {path}")
    if sum(bucket.point_count for bucket in cross_buckets) != test.point_count:
        raise ScaffoldError(
            f"scorecard.cross_window 样本与 OOS test 不一致: {path}"
        )
    if (
        sum(bucket.payload["sample_n"] for bucket in cross_buckets)
        != test.payload["sample_n"]
    ):
        raise ScaffoldError(
            f"scorecard.cross_window return 与 OOS test 不一致: {path}"
        )
    for previous, current in zip(cross_buckets, cross_buckets[1:]):
        if (
            previous.end is None
            or current.start is None
            or _timedelta_microseconds(previous.end, current.start)
            != timeframe_us
        ):
            raise ScaffoldError(f"scorecard.cross_window 时间分区不闭合: {path}")
    if (
        cross_buckets[0].start != test.start
        or cross_buckets[-1].end != test.end
    ):
        raise ScaffoldError(f"scorecard.cross_window 与 OOS test 覆盖范围不一致: {path}")

    cost = _require_exact_mapping(
        data.get("cost_adjusted"),
        keys=SCORECARD_COST_ADJUSTED_KEYS,
        label="cost_adjusted",
        path=path,
    )
    overall_cost = _validate_cost_bucket(
        {key: cost[key] for key in SCORECARD_COST_BUCKET_KEYS},
        label="cost_adjusted.overall",
        path=path,
        fill_count=total_fills,
    )
    train_cost = _validate_cost_bucket(
        cost["train"],
        label="cost_adjusted.train",
        path=path,
        fill_count=train.payload["fills"],
    )
    test_cost = _validate_cost_bucket(
        cost["test"],
        label="cost_adjusted.test",
        path=path,
        fill_count=test.payload["fills"],
    )
    sensitivity = _require_exact_mapping(
        cost["sensitivity"],
        keys=SCORECARD_SENSITIVITY_KEYS,
        label="cost_adjusted.sensitivity",
        path=path,
    )
    for name, bucket, fill_count in (
        ("overall", overall_cost, total_fills),
        ("train", train_cost, train.payload["fills"]),
        ("test", test_cost, test.payload["fills"]),
    ):
        _validate_cost_sensitivity(
            bucket,
            sensitivity[name],
            label=f"cost_adjusted.sensitivity.{name}",
            path=path,
            fill_count=fill_count,
        )
    if total_fills > 0:
        train_fills = train.payload["fills"]
        test_fills = test.payload["fills"]
        for key in SCORECARD_COST_BUCKET_KEYS:
            expected = (
                train_cost[key] * train_fills + test_cost[key] * test_fills
            ) / total_fills
            _require_metric_close(
                overall_cost[key],
                expected,
                label=f"cost_adjusted.overall.{key}",
                path=path,
            )

    regime = _require_exact_mapping(
        data.get("regime_slice"),
        keys=SCORECARD_REGIME_KEYS,
        label="regime_slice",
        path=path,
    )
    vol = _require_exact_mapping(
        regime["vol"],
        keys=SCORECARD_VOL_KEYS,
        label="regime_slice.vol",
        path=path,
    )
    regime_buckets: list[dict[str, Any]] = []
    for name in SCORECARD_VOL_KEYS:
        bucket = _require_exact_mapping(
            vol[name],
            keys=SCORECARD_REGIME_BUCKET_KEYS,
            label=f"regime_slice.vol.{name}",
            path=path,
        )
        _require_metric_number(
            bucket["ir"],
            label=f"regime_slice.vol.{name}.ir",
            path=path,
        )
        for key in ("fills", "sample_n"):
            if type(bucket[key]) is not int or bucket[key] < 0:
                raise ScaffoldError(
                    f"scorecard.regime_slice.vol.{name}.{key} 无效: {path}"
                )
        if bucket["sample_n"] < 2 and bucket["ir"] != 0:
            raise ScaffoldError(
                f"scorecard.regime_slice.vol.{name}.ir 样本不足时必须为零: {path}"
            )
        if bucket["fills"] > bucket["sample_n"]:
            raise ScaffoldError(
                f"scorecard.regime_slice.vol.{name}.fills 超过样本数: {path}"
            )
        regime_buckets.append(bucket)

    if sum(bucket["sample_n"] for bucket in regime_buckets) != total_bars - 1:
        raise ScaffoldError(
            f"scorecard.regime_slice sample 分区与 meta.total_bars 不一致: {path}"
        )

    if (
        train.payload["fills"] + test.payload["fills"] != total_fills
        or sum(bucket.payload["fills"] for bucket in cross_buckets)
        != test.payload["fills"]
        or sum(bucket["fills"] for bucket in regime_buckets) != total_fills
    ):
        raise ScaffoldError(f"scorecard fill 分区与 meta.total_fills 不一致: {path}")


def _validate_scorecard_contract(data: dict[str, Any], *, path: Path) -> None:
    """Reject legacy or unit-ambiguous scorecards before creating a bundle."""

    if data.get("artifact_kind") != SCORECARD_ARTIFACT_KIND:
        raise ScaffoldError(f"scorecard artifact_kind 不受支持: {path}")
    if data.get("artifact_schema_version") != SCORECARD_SCHEMA_VERSION:
        raise ScaffoldError(f"scorecard artifact_schema_version 不受支持: {path}")
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise ScaffoldError(f"scorecard.meta 必须是 JSON 对象: {path}")
    if set(meta) != _SCORECARD_META_KEYS:
        raise ScaffoldError(f"scorecard.meta schema 不完整或含未知字段: {path}")
    expected_meta = {
        "execution_model_version": "next_bar_event_v2",
        "fill_model_version": SCORECARD_FILL_MODEL_VERSION,
        "instrument_arithmetic_policy_id": INSTRUMENT_ARITHMETIC_POLICY_ID,
        "contract_lineage_status": "calculation_contract_only_unverified",
        "risk_metric_policy_id": REPLAY_RISK_METRIC_POLICY_ID,
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            raise ScaffoldError(f"scorecard.meta.{key} 不受支持: {path}")
    if meta.get("spot_buy_fee_asset") not in {"base", "quote"}:
        raise ScaffoldError(
            f"scorecard.meta.spot_buy_fee_asset 不受支持: {path}"
        )
    family = meta.get("family")
    if not isinstance(family, str) or family not in _BUILTIN_ADAPTER_CONTRACTS:
        raise ScaffoldError(f"scorecard.meta.family 不受 Route A 支持: {path}")
    adapter_contract = _BUILTIN_ADAPTER_CONTRACTS[family]
    if (
        meta.get("adapter_identity"),
        meta.get("adapter_algorithm_version"),
    ) != adapter_contract:
        raise ScaffoldError(f"scorecard adapter 与 family/version 不一致: {path}")
    order_type = meta.get("order_type")
    if not isinstance(order_type, str) or order_type not in {
        "ioc",
        "post_only",
        "bounded_limit",
    }:
        raise ScaffoldError(f"scorecard.meta.order_type 不受支持: {path}")
    timeframe_ms = _parse_scorecard_timeframe_ms(meta.get("timeframe"), path=path)
    if (
        not isinstance(meta.get("dataset_version"), str)
        or not meta["dataset_version"].strip()
    ):
        raise ScaffoldError(f"scorecard.meta.dataset_version 无效: {path}")
    if meta.get("market_data_granularity") != _EXPECTED_MARKET_DATA_GRANULARITY:
        raise ScaffoldError(
            f"scorecard.meta.market_data_granularity 不受支持: {path}"
        )
    if (
        meta.get("execution_realism_limitations")
        != _EXPECTED_EXECUTION_REALISM_LIMITATIONS
    ):
        raise ScaffoldError(
            f"scorecard.meta.execution_realism_limitations 不受支持: {path}"
        )
    if meta.get("fill_attribution_status") != "explicit_v1":
        raise ScaffoldError(f"scorecard fill attribution 不可用于 Route A: {path}")
    if type(meta.get("cadence_gap_count")) is not int:
        raise ScaffoldError(f"scorecard.meta.cadence_gap_count 无效: {path}")
    if meta["cadence_gap_count"] != 0:
        raise ScaffoldError(f"scorecard 存在 cadence gap，不可进入 Route A: {path}")
    for key in ("total_bars", "total_fills", "total_decisions"):
        if type(meta.get(key)) is not int or meta[key] < 0:
            raise ScaffoldError(f"scorecard.meta.{key} 无效: {path}")
    if meta["total_fills"] > meta["total_decisions"]:
        raise ScaffoldError(f"scorecard.meta fill/decision 数量无效: {path}")
    if meta["total_decisions"] != meta["total_bars"]:
        raise ScaffoldError(f"scorecard.meta decision/bar 数量不一致: {path}")
    start_ts = _parse_utc_iso_or_none(meta["start_ts"], label="meta.start_ts", path=path)
    end_ts = _parse_utc_iso_or_none(meta["end_ts"], label="meta.end_ts", path=path)
    generated_at = _parse_utc_iso_or_none(
        meta["generated_at"],
        label="meta.generated_at",
        path=path,
    )
    if start_ts is None or end_ts is None or start_ts >= end_ts:
        raise ScaffoldError(f"scorecard.meta replay window 无效: {path}")
    if generated_at is None or generated_at < end_ts:
        raise ScaffoldError(f"scorecard.meta.generated_at 早于 replay window: {path}")
    if meta["total_bars"] == 0:
        raise ScaffoldError(f"scorecard.meta.total_bars 不得为零: {path}")
    if meta["total_fills"] > meta["total_bars"] - 1:
        raise ScaffoldError(f"scorecard.meta fills 超过可归因 bar interval: {path}")
    resolved_parameters = meta.get("resolved_parameters")
    if (
        not isinstance(resolved_parameters, dict)
        or set(resolved_parameters) != _RESOLVED_PARAMETER_KEYS
        or resolved_parameters.get("extra") != {}
        or resolved_parameters.get("strategy_short_bias_enabled") is not False
    ):
        raise ScaffoldError(f"scorecard resolved_parameters schema 无效: {path}")
    resolved_cost = resolved_parameters.get("cost_config")
    if not isinstance(resolved_cost, dict) or set(resolved_cost) != (
        _RESOLVED_COST_CONFIG_KEYS
    ):
        raise ScaffoldError(f"scorecard resolved cost schema 无效: {path}")
    try:
        parameter_payload = {
            key: value
            for key, value in resolved_parameters.items()
            if key != "extra"
        }
        reconstructed_parameters = ReplayParameterOverrides.from_dict(
            parameter_payload,
            base=ReplayParameterOverrides.for_family(meta.get("family")),
        )
    except (TypeError, ValueError) as exc:
        raise ScaffoldError(f"scorecard resolved_parameters 数值无效: {path}") from exc
    if asdict(reconstructed_parameters) != resolved_parameters:
        raise ScaffoldError(f"scorecard resolved_parameters 类型或值无效: {path}")

    contract = meta.get("instrument_contract")
    if not isinstance(contract, dict):
        raise ScaffoldError(f"scorecard.meta.instrument_contract 必须是对象: {path}")
    if set(contract) != _CONTRACT_REQUIRED_KEYS:
        raise ScaffoldError(f"scorecard instrument_contract schema 不完整或含未知字段: {path}")
    try:
        resolved_contract = InstrumentContract(
            symbol=contract["symbol"],
            instrument_type=contract["instrument_type"],
            contract_type=contract["contract_type"],
            base_currency=contract["base_currency"],
            quote_currency=contract["quote_currency"],
            settle_currency=contract["settle_currency"],
            contract_value=Decimal(str(contract["contract_value"])),
            contract_multiplier=Decimal(str(contract["contract_multiplier"])),
            contract_value_currency=contract["contract_value_currency"],
            lot_size=Decimal(str(contract["lot_size"])),
            min_size=Decimal(str(contract["min_size"])),
            tick_size=Decimal(str(contract["tick_size"])),
        )
    except (InstrumentContractError, InvalidOperation, TypeError, ValueError) as exc:
        raise ScaffoldError(f"scorecard instrument_contract 无效: {path}") from exc
    if (
        resolved_contract.instrument_type != "SPOT"
        or resolved_contract.contract_type != "spot"
    ):
        raise ScaffoldError(f"scorecard 当前只允许 SPOT contract: {path}")
    if resolved_contract.symbol != meta.get("symbol"):
        raise ScaffoldError(f"scorecard contract symbol 与 meta 不一致: {path}")
    if meta.get("instrument_contract_fingerprint") != resolved_contract.fingerprint:
        raise ScaffoldError(f"scorecard contract fingerprint 与声明不一致: {path}")
    if (
        not isinstance(meta.get("settlement_currency"), str)
        or not meta["settlement_currency"]
        or resolved_contract.settle_currency != meta.get("settlement_currency")
    ):
        raise ScaffoldError(f"scorecard settlement currency lineage 无效: {path}")
    _validate_nested_scorecard_contract(
        data,
        path=path,
        meta_start=start_ts,
        meta_end=end_ts,
        timeframe_ms=timeframe_ms,
        total_bars=meta["total_bars"],
        total_fills=meta["total_fills"],
    )
    _reject_decision_language(data, path=path)


def _validate_observation_window_contract(
    data: dict[str, Any],
    *,
    path: Path,
) -> None:
    """Accept one exact, internally consistent observation-window v1 artifact."""

    if data.get("artifact_kind") != OBSERVATION_WINDOW_ARTIFACT_KIND:
        raise ScaffoldError(f"observation-window artifact_kind 不受支持: {path}")
    if data.get("artifact_schema_version") != OBSERVATION_WINDOW_SCHEMA_VERSION:
        raise ScaffoldError(
            f"observation-window artifact_schema_version 不受支持: {path}"
        )
    for key in ("generated_at", "window_start", "window_target"):
        value = data.get(key)
        if not isinstance(value, str):
            raise ScaffoldError(f"observation-window {key} 无效: {path}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScaffoldError(f"observation-window {key} 无效: {path}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ScaffoldError(f"observation-window {key} 必须是 UTC: {path}")
    window_start = datetime.fromisoformat(
        data["window_start"].replace("Z", "+00:00")
    )
    window_target = datetime.fromisoformat(
        data["window_target"].replace("Z", "+00:00")
    )
    if window_start >= window_target:
        raise ScaffoldError(f"observation-window 时间窗口无效: {path}")
    generated_at = datetime.fromisoformat(
        data["generated_at"].replace("Z", "+00:00")
    )
    if generated_at < window_start:
        raise ScaffoldError(f"observation-window generated_at 早于窗口起点: {path}")

    overall = data.get("overall")
    exit_code = data.get("exit_code")
    if type(exit_code) is not int:
        raise ScaffoldError(f"observation-window exit_code 无效: {path}")
    for key in ("warn_count", "fail_count"):
        if type(data.get(key)) is not int or data[key] < 0:
            raise ScaffoldError(f"observation-window {key} 无效: {path}")
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise ScaffoldError(f"observation-window checks 必须是数组: {path}")
    for check in checks:
        if not isinstance(check, dict) or set(check) != {
            "section",
            "status",
            "message",
        }:
            raise ScaffoldError(f"observation-window checks schema 无效: {path}")
        if check.get("status") not in {"pass", "warn", "fail"}:
            raise ScaffoldError(f"observation-window check status 无效: {path}")
        if not isinstance(check.get("section"), str) or not isinstance(
            check.get("message"),
            str,
        ):
            raise ScaffoldError(f"observation-window check 文本字段无效: {path}")
    if data["warn_count"] != sum(c.get("status") == "warn" for c in checks):
        raise ScaffoldError(f"observation-window warn_count 不一致: {path}")
    if data["fail_count"] != sum(c.get("status") == "fail" for c in checks):
        raise ScaffoldError(f"observation-window fail_count 不一致: {path}")
    if data["fail_count"] > 0:
        expected_overall, expected_exit = "fail", 2
    elif data["warn_count"] > 0:
        expected_overall, expected_exit = "pass_with_warn", 1
    else:
        expected_overall, expected_exit = "pass", 0
    if overall != expected_overall or exit_code != expected_exit:
        raise ScaffoldError(f"observation-window overall/exit_code 不一致: {path}")


def _reject_decision_language(value: Any, *, path: Path) -> None:
    """Keep human gate conclusions out of copied machine scorecards."""

    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = "".join(
                char for char in str(key).strip().lower() if char.isalnum()
            )
            if normalized_key in _FORBIDDEN_DECISION_TOKENS:
                raise ScaffoldError(f"scorecard 含自动决策字段 {key!r}: {path}")
            _reject_decision_language(child, path=path)
    elif isinstance(value, list):
        for child in value:
            _reject_decision_language(child, path=path)
    elif isinstance(value, str):
        normalized_value = "".join(
            char for char in value.strip().lower() if char.isalnum()
        )
        word_tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", value.strip().lower())
            if token
        }
        if (
            normalized_value in _FORBIDDEN_DECISION_TOKENS
            or word_tokens & _FORBIDDEN_DECISION_TOKENS
        ):
            raise ScaffoldError(f"scorecard 含自动决策值 {value!r}: {path}")


def _to_utc_iso(ts: datetime) -> str:
    aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


_TBD = "<TBD>"


def _fmt_scalar(value: Any) -> str:
    """渲染标量为表格值; None / "" / 缺字段 → <TBD>。

    浮点统一 ``%.6g`` 以保留显著位; 整数/字符串 / bool 按 ``str`` 渲染。
    """
    if value is None:
        return _TBD
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return _TBD
        return f"{value:.6g}"
    if isinstance(value, str) and value == "":
        return _TBD
    return str(value)


def _render_metadata_table(
    inputs: ScaffoldInputs,
    *,
    generated_iso: str,
    scorecard: dict[str, Any],
) -> str:
    """元数据 (提案头) — 从 CLI inputs + scorecard.meta + scorecard.oos 预填。"""
    proposer = inputs.proposer or _TBD
    meta = scorecard.get("meta") if isinstance(scorecard.get("meta"), dict) else {}
    oos = scorecard.get("oos") if isinstance(scorecard.get("oos"), dict) else {}

    symbol = _fmt_scalar(meta.get("symbol"))
    timeframe = _fmt_scalar(meta.get("timeframe"))
    dataset_version = _fmt_scalar(meta.get("dataset_version"))
    order_type = _fmt_scalar(meta.get("order_type"))
    schema_version = _fmt_scalar(scorecard.get("artifact_schema_version"))
    lineage_status = _fmt_scalar(meta.get("contract_lineage_status"))

    start_ts = meta.get("start_ts")
    end_ts = meta.get("end_ts")
    split_ts = oos.get("split_ts")
    if start_ts or end_ts or split_ts:
        time_range = (
            f"`{_fmt_scalar(start_ts)}` → `{_fmt_scalar(end_ts)}` "
            f"(split @ `{_fmt_scalar(split_ts)}`)"
        )
    else:
        time_range = _TBD

    rows = [
        ("提案 ID", f"`{inputs.proposal_id}`"),
        ("提案日期", f"`{generated_iso}`"),
        ("提案人", f"`{proposer}`"),
        ("Scope: feature", f"`{inputs.feature}`"),
        ("Scope: horizon", f"`{inputs.horizon}`"),
        ("Scope: symbol", f"`{symbol}`"),
        ("Scope: timeframe", f"`{timeframe}`"),
        ("Scope: dataset_version", f"`{dataset_version}`"),
        ("Scope: order_type", f"`{order_type}`"),
        ("Artifact schema", f"`{schema_version}`"),
        ("Contract lineage", f"`{lineage_status}`"),
        ("Scope: time range", time_range),
    ]
    lines = ["## 元数据 (提案头)", "", "| 字段 | 值 |", "|---|---|"]
    for k, v in rows:
        lines.append(f"| {k} | {v} |")
    lines.append("")
    return "\n".join(lines)


def _render_train_test_split(scorecard: dict[str, Any]) -> str:
    """§4 Train / Test 分割 — 只写事实, 不写'边界理由'判断。"""
    oos = scorecard.get("oos") if isinstance(scorecard.get("oos"), dict) else {}
    train = oos.get("train") if isinstance(oos.get("train"), dict) else {}
    test = oos.get("test") if isinstance(oos.get("test"), dict) else {}

    lines = [
        "## §4 Train / Test 分割",
        "",
        "从 `scorecard.oos` 预填 (时间边界 / 切分方法), 边界理由等定性分析"
        "仍需人工补写。",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| train_start | `{_fmt_scalar(train.get('start'))}` |",
        f"| train_end | `{_fmt_scalar(train.get('end'))}` |",
        f"| test_start | `{_fmt_scalar(test.get('start'))}` |",
        f"| test_end | `{_fmt_scalar(test.get('end'))}` |",
        f"| split_method | `{_fmt_scalar(oos.get('split_method'))}` |",
        f"| split_ts | `{_fmt_scalar(oos.get('split_ts'))}` |",
        "",
        "**待填**: 分割理由 / 是否跨已知 regime 切换 (见模板 §4)。",
        "",
    ]
    return "\n".join(lines)


def _render_oos_table(scorecard: dict[str, Any]) -> str:
    """§6.1 OOS — 从 scorecard.oos.train/test 预填, 不写判据结论。"""
    oos = scorecard.get("oos") if isinstance(scorecard.get("oos"), dict) else {}
    train = oos.get("train") if isinstance(oos.get("train"), dict) else {}
    test = oos.get("test") if isinstance(oos.get("test"), dict) else {}

    def _cell(bucket: dict[str, Any], key: str) -> str:
        return _fmt_scalar(bucket.get(key))

    lines = [
        "### §6.1 OOS (预填原始值)",
        "",
        "| 指标 | train | test |",
        "|---|---|---|",
        f"| IR (annualized) | {_cell(train, 'ir_annualized')} | "
        f"{_cell(test, 'ir_annualized')} |",
        f"| Sharpe | {_cell(train, 'sharpe_ratio')} | "
        f"{_cell(test, 'sharpe_ratio')} |",
        f"| Hit rate | {_cell(train, 'hit_rate')} | "
        f"{_cell(test, 'hit_rate')} |",
        f"| Max drawdown (bps) | {_cell(train, 'max_drawdown_bps')} | "
        f"{_cell(test, 'max_drawdown_bps')} |",
        f"| Sample N | {_cell(train, 'sample_n')} | "
        f"{_cell(test, 'sample_n')} |",
        "",
        "**待填**: 判据结论 / 累计曲线图路径 (见模板 §6.1)。",
        "",
    ]
    return "\n".join(lines)


def _render_cross_window_table(scorecard: dict[str, Any]) -> str:
    """§6.2 Cross-window — 自动按 S1/S2/S3... label 列出所有 slice 原始值。"""
    cross = scorecard.get("cross_window")
    slices: list[dict[str, Any]] = (
        [s for s in cross if isinstance(s, dict)] if isinstance(cross, list) else []
    )

    lines = [
        "### §6.2 Cross-window (预填原始值)",
        "",
        "| Slice | 时间范围 | IR (annualized) | Hit rate | Max DD (bps) | Sample N |",
        "|---|---|---|---|---|---|",
    ]
    if not slices:
        lines.append("| <TBD> | <TBD> | <TBD> | <TBD> | <TBD> | <TBD> |")
    else:
        for idx, slc in enumerate(slices, start=1):
            label = f"S{idx}"
            start = _fmt_scalar(slc.get("start"))
            end = _fmt_scalar(slc.get("end"))
            ir = _fmt_scalar(slc.get("ir_annualized"))
            hit = _fmt_scalar(slc.get("hit_rate"))
            dd = _fmt_scalar(slc.get("max_drawdown_bps"))
            n = _fmt_scalar(slc.get("sample_n"))
            lines.append(
                f"| {label} | `{start}` → `{end}` | {ir} | {hit} | {dd} | {n} |"
            )
    lines.extend(
        [
            "",
            "**待填**: 判据结论 / 切片对比图 (见模板 §6.2)。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_cost_adjusted_table(scorecard: dict[str, Any]) -> str:
    """§6.3 Cost-adjusted — train/test 5 字段 + sensitivity 原始值, 不写结论。"""
    cost = (
        scorecard.get("cost_adjusted")
        if isinstance(scorecard.get("cost_adjusted"), dict)
        else {}
    )
    train = cost.get("train") if isinstance(cost.get("train"), dict) else {}
    test = cost.get("test") if isinstance(cost.get("test"), dict) else {}
    sens = (
        cost.get("sensitivity") if isinstance(cost.get("sensitivity"), dict) else {}
    )
    sens_train = sens.get("train") if isinstance(sens.get("train"), dict) else {}
    sens_test = sens.get("test") if isinstance(sens.get("test"), dict) else {}

    def _cell(bucket: dict[str, Any], key: str) -> str:
        return _fmt_scalar(bucket.get(key))

    lines = [
        "### §6.3 Cost-adjusted (预填原始值)",
        "",
        "| 字段 | train | test |",
        "|---|---|---|",
        f"| realized_edge_bps | {_cell(train, 'realized_edge_bps')} | "
        f"{_cell(test, 'realized_edge_bps')} |",
        f"| fee_bps | {_cell(train, 'fee_bps')} | {_cell(test, 'fee_bps')} |",
        f"| slip_bps | {_cell(train, 'slip_bps')} | {_cell(test, 'slip_bps')} |",
        f"| exec_buffer_bps | {_cell(train, 'exec_buffer_bps')} | "
        f"{_cell(test, 'exec_buffer_bps')} |",
        f"| net_edge_bps | {_cell(train, 'net_edge_bps')} | "
        f"{_cell(test, 'net_edge_bps')} |",
        "",
        "**Sensitivity 原始值** (仅列值, 不判断是否仍 > 0):",
        "",
        "| 压力情景 | train | test |",
        "|---|---|---|",
        f"| fee 上调 20% 后 net_edge_bps | {_cell(sens_train, 'net_edge_fee_up_20pct_bps')}"
        f" | {_cell(sens_test, 'net_edge_fee_up_20pct_bps')} |",
        f"| slip +0.5 bps 后 net_edge_bps | {_cell(sens_train, 'net_edge_slip_plus_0_5bps_bps')}"
        f" | {_cell(sens_test, 'net_edge_slip_plus_0_5bps_bps')} |",
        "",
        "**待填**: 判据结论 (见模板 §6.3)。",
        "",
    ]
    return "\n".join(lines)


def _render_regime_slice_table(scorecard: dict[str, Any]) -> str:
    """§6.4 Regime-slice — 从 ``scorecard.regime_slice.vol.low/high`` 预填原始值。

    当前只覆盖 vol 单维切片 (low_vol / high_vol); funding 方向 / 2×2 heatmap
    仍需人工补或后续迭代。缺字段用 ``<TBD>`` 占位, 不写任何判据结论。
    """
    regime = (
        scorecard.get("regime_slice")
        if isinstance(scorecard.get("regime_slice"), dict)
        else {}
    )
    vol = regime.get("vol") if isinstance(regime.get("vol"), dict) else {}
    low = vol.get("low") if isinstance(vol.get("low"), dict) else {}
    high = vol.get("high") if isinstance(vol.get("high"), dict) else {}

    def _cell(bucket: dict[str, Any], key: str) -> str:
        return _fmt_scalar(bucket.get(key))

    lines = [
        "### §6.4 Regime-slice (预填原始值)",
        "",
        "当前自动预填只覆盖 **vol 单维切片**; funding 方向 / 2×2 heatmap 仍需"
        "人工补或后续迭代。此处只列原始值, 不给结论。",
        "",
        "| bucket | IR | fills | sample_n |",
        "|---|---|---|---|",
        f"| low_vol | {_cell(low, 'ir')} | {_cell(low, 'fills')} | "
        f"{_cell(low, 'sample_n')} |",
        f"| high_vol | {_cell(high, 'ir')} | {_cell(high, 'fills')} | "
        f"{_cell(high, 'sample_n')} |",
        "",
        "**待填**: funding 方向切片 / 2×2 heatmap / 判据结论 (见模板 §6.4)。",
        "",
    ]
    return "\n".join(lines)


def _render_observation_window_summary(observation: dict[str, Any]) -> str:
    """观察窗摘要 — 把 candidate evidence 与观察窗真相层绑在一起。"""
    lines = [
        "## 观察窗摘要 (引用 `observation_window_summary.json`)",
        "",
        "**观察窗状态：`incomplete_single_snapshot`。本 bundle 只包含一份 daily "
        "snapshot，不能证明连续 7 天完成，也不构成人工批准或资本资格。**",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| generated_at | `{_fmt_scalar(observation.get('generated_at'))}` |",
        "| observation_completion_status | `incomplete_single_snapshot` |",
        f"| overall | `{_fmt_scalar(observation.get('overall'))}` |",
        f"| window_start | `{_fmt_scalar(observation.get('window_start'))}` |",
        f"| window_target | `{_fmt_scalar(observation.get('window_target'))}` |",
        f"| warn_count | {_fmt_scalar(observation.get('warn_count'))} |",
        f"| fail_count | {_fmt_scalar(observation.get('fail_count'))} |",
        "",
    ]
    return "\n".join(lines)


def _render_proposal_md(
    inputs: ScaffoldInputs,
    *,
    generated_iso: str,
    source_paths: dict[str, str],
    scorecard: dict[str, Any],
    observation: dict[str, Any],
) -> str:
    header = (
        f"# 路线 A Phase 0 · Evidence 提案 · `{inputs.feature}` @ `{inputs.horizon}`\n"
        "\n"
        "> 本文件由 `aats.cli route-a-evidence-scaffold` 生成，预填已具备数据来源"
        "的段落 (metadata / §4 / §6.1 / §6.2 / §6.3 / §6.4 vol 切片 / 观察窗摘要)。\n"
        "> 完整提案骨架请参照 "
        "`docs/research/_templates/route_a_phase0_evidence_template.md` 逐段填写。"
        "本脚手架**不**输出任何判据结论 / 归档裁决文案, 以保持数值追溯口径。\n"
        "> 当前 bundle 仅含单份 daily snapshot，观察窗仍为 "
        "`incomplete_single_snapshot`；不能证明连续 7 天完成，也不构成人工批准或"
        "资本资格。\n"
        "\n"
    )
    artifact = (
        "## 已复制 artifact\n"
        "\n"
        f"- `{_MANIFEST_NAME}` — bundle 元数据 + source provenance (sha256)\n"
        f"- `{_COPIED_SCORECARD_NAME}` — 复制自 "
        f"`{source_paths['scorecard_json']}`\n"
        f"- `{_COPIED_OBSERVATION_NAME}` — 复制自 "
        f"`{source_paths['observation_window_json']}`\n"
        "\n"
    )
    hardcoded_sections_header = "## §6 四条硬指标实际数据 (预填原始值)\n\n"
    todo_tail = (
        "## 待人工填写\n"
        "\n"
        "以下段落当前无机械数据来源, 需按模板 "
        "`docs/research/_templates/route_a_phase0_evidence_template.md` 手填:\n"
        "\n"
        "- §1 数据源 (表 + 时间范围 + 样本 N + 过滤条件 + snapshot hash)\n"
        "- §2 特征定义 / 特征统计表\n"
        "- §3 模型定义 / 默认超参 / sweep 说明\n"
        "- §4 分割理由 / 是否跨 regime 切换 (本脚手架只写事实边界)\n"
        "- §5 Cost model 引用 (fee_resolver commit hash + governance 当前值)\n"
        "- §6.4 Regime-slice funding 方向切片 / 2×2 heatmap (本脚手架仅预填 vol 单维)\n"
        "- §6.1 / §6.2 / §6.3 / §6.4 判据结论与图表路径\n"
        "- §7 加分项 (physical plausibility / cross-symbol / neighborhood / replay / "
        "independent re-run)\n"
        "- §8 反模式 red flag 自查\n"
        "- §9~§15 结论 / 回退预案 / 可复现性证据 / cross-check / 决策 audit / "
        "附录 / 签署\n"
    )
    return (
        header
        + _render_metadata_table(
            inputs, generated_iso=generated_iso, scorecard=scorecard
        )
        + "\n"
        + artifact
        + _render_train_test_split(scorecard)
        + "\n"
        + hardcoded_sections_header
        + _render_oos_table(scorecard)
        + "\n"
        + _render_cross_window_table(scorecard)
        + "\n"
        + _render_cost_adjusted_table(scorecard)
        + "\n"
        + _render_regime_slice_table(scorecard)
        + "\n"
        + _render_observation_window_summary(observation)
        + "\n"
        + todo_tail
    )


def create_scaffold(
    inputs: ScaffoldInputs,
    *,
    generated_at: datetime | None = None,
) -> ScaffoldResult:
    """Create a Route A phase 0 evidence bundle scaffold on disk.

    Raises:
        ScaffoldError: 输入文件缺失 / JSON 顶层字段缺失 / 输出目录已存在。
    """
    if generated_at is None:
        generated_at = datetime.now(tz=timezone.utc)
    generated_iso = _to_utc_iso(generated_at)

    proposal_id = str(inputs.proposal_id or "")
    proposal_component = Path(proposal_id)
    if (
        not proposal_id
        or proposal_id in {".", ".."}
        or proposal_component.is_absolute()
        or proposal_component.name != proposal_id
        or "/" in proposal_id
        or "\\" in proposal_id
    ):
        raise ScaffoldError("proposal_id 必须是非空单段路径名称")

    output_root = Path(inputs.output_root).resolve(strict=False)
    proposal_dir = (output_root / proposal_id).resolve(strict=False)
    if proposal_dir.parent != output_root:
        raise ScaffoldError("proposal_id 超出 output_root，拒绝创建")

    scorecard_src = Path(inputs.scorecard_json)
    observation_src = Path(inputs.observation_window_json)

    scorecard_data = _load_and_validate_json(
        scorecard_src, SCORECARD_REQUIRED_KEYS, label="scorecard"
    )
    _validate_scorecard_contract(scorecard_data, path=scorecard_src)
    observation_data = _load_and_validate_json(
        observation_src,
        OBSERVATION_WINDOW_REQUIRED_KEYS,
        label="observation-window",
    )
    _validate_observation_window_contract(
        observation_data,
        path=observation_src,
    )

    if proposal_dir.exists():
        raise ScaffoldError(
            f"proposal 目录已存在，拒绝覆盖 (保留审计轨迹): {proposal_dir}"
        )

    proposal_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{proposal_dir.name}.staging-",
            dir=proposal_dir.parent,
        )
    )
    try:
        staged_scorecard = staging_dir / _COPIED_SCORECARD_NAME
        staged_observation = staging_dir / _COPIED_OBSERVATION_NAME
        shutil.copyfile(scorecard_src, staged_scorecard)
        shutil.copyfile(observation_src, staged_observation)
        copied_scorecard = _load_and_validate_json(
            staged_scorecard,
            SCORECARD_REQUIRED_KEYS,
            label="copied scorecard",
        )
        _validate_scorecard_contract(copied_scorecard, path=staged_scorecard)
        copied_observation = _load_and_validate_json(
            staged_observation,
            OBSERVATION_WINDOW_REQUIRED_KEYS,
            label="copied observation-window",
        )
        _validate_observation_window_contract(
            copied_observation,
            path=staged_observation,
        )
        if copied_scorecard != scorecard_data or copied_observation != observation_data:
            raise ScaffoldError("输入在校验与复制之间发生变化，拒绝生成 bundle")

        source_paths = {
            "scorecard_json": str(scorecard_src),
            "observation_window_json": str(observation_src),
        }
        source_sha256 = {
            "scorecard_json": _sha256_of_file(staged_scorecard),
            "observation_window_json": _sha256_of_file(staged_observation),
        }
        staged_proposal = staging_dir / _PROPOSAL_MD_NAME
        staged_proposal.write_text(
            _render_proposal_md(
                inputs,
                generated_iso=generated_iso,
                source_paths=source_paths,
                scorecard=scorecard_data,
                observation=observation_data,
            ),
            encoding="utf-8",
            newline="\n",
        )

        manifest: dict[str, Any] = {
            "artifact_kind": BUNDLE_MANIFEST_ARTIFACT_KIND,
            "artifact_schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
            # This certifies atomic publication only.  A single daily snapshot
            # cannot prove the required natural-time observation window, and
            # proposal.md intentionally still contains human-owned <TBD>s.
            "artifact_set_complete": False,
            "observation_completion_status": "incomplete_single_snapshot",
            "proposal_id": inputs.proposal_id,
            "feature": inputs.feature,
            "horizon": inputs.horizon,
            "proposer": inputs.proposer,
            "generated_at": generated_iso,
            "scorecard_contract": {
                "artifact_kind": scorecard_data["artifact_kind"],
                "artifact_schema_version": scorecard_data[
                    "artifact_schema_version"
                ],
                "contract_lineage_status": scorecard_data["meta"][
                    "contract_lineage_status"
                ],
            },
            "observation_window_contract": {
                "artifact_kind": observation_data["artifact_kind"],
                "artifact_schema_version": observation_data[
                    "artifact_schema_version"
                ],
            },
            "source_paths": source_paths,
            "source_sha256": source_sha256,
            "artifacts": {
                "manifest": _MANIFEST_NAME,
                "scorecard": _COPIED_SCORECARD_NAME,
                "observation_window_summary": _COPIED_OBSERVATION_NAME,
                "proposal_md": _PROPOSAL_MD_NAME,
            },
        }
        (staging_dir / _MANIFEST_NAME).write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(staging_dir, proposal_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise

    return ScaffoldResult(
        proposal_dir=proposal_dir,
        manifest_path=proposal_dir / _MANIFEST_NAME,
        scorecard_path=proposal_dir / _COPIED_SCORECARD_NAME,
        observation_window_summary_path=(
            proposal_dir / _COPIED_OBSERVATION_NAME
        ),
        proposal_md_path=proposal_dir / _PROPOSAL_MD_NAME,
    )


__all__ = [
    "BUNDLE_MANIFEST_ARTIFACT_KIND",
    "BUNDLE_MANIFEST_SCHEMA_VERSION",
    "DEFAULT_OUTPUT_ROOT",
    "OBSERVATION_WINDOW_ARTIFACT_KIND",
    "OBSERVATION_WINDOW_REQUIRED_KEYS",
    "OBSERVATION_WINDOW_SCHEMA_VERSION",
    "SCORECARD_REQUIRED_KEYS",
    "ScaffoldError",
    "ScaffoldInputs",
    "ScaffoldResult",
    "create_scaffold",
]
