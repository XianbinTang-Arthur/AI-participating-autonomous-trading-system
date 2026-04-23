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
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCORECARD_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"meta", "oos", "cross_window", "cost_adjusted", "regime_slice"}
)
OBSERVATION_WINDOW_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
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

DEFAULT_OUTPUT_ROOT: Path = Path("docs/research/route_a_phase0")

_COPIED_SCORECARD_NAME = "scorecard.json"
_COPIED_OBSERVATION_NAME = "observation_window_summary.json"
_MANIFEST_NAME = "manifest.json"
_PROPOSAL_MD_NAME = "proposal.md"


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
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScaffoldError(
            f"{label} JSON 解析失败: {path} ({exc.msg} @ line {exc.lineno})"
        ) from exc
    if not isinstance(data, dict):
        raise ScaffoldError(f"{label} 顶层必须是 JSON 对象: {path}")
    missing = sorted(required_keys - data.keys())
    if missing:
        raise ScaffoldError(
            f"{label} 缺少必需顶层字段: {missing} (path={path})"
        )
    return data


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
        "| 字段 | 值 |",
        "|---|---|",
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

    scorecard_src = Path(inputs.scorecard_json)
    observation_src = Path(inputs.observation_window_json)

    scorecard_data = _load_and_validate_json(
        scorecard_src, SCORECARD_REQUIRED_KEYS, label="scorecard"
    )
    observation_data = _load_and_validate_json(
        observation_src,
        OBSERVATION_WINDOW_REQUIRED_KEYS,
        label="observation-window",
    )

    proposal_dir = Path(inputs.output_root) / inputs.proposal_id
    if proposal_dir.exists():
        raise ScaffoldError(
            f"proposal 目录已存在，拒绝覆盖 (保留审计轨迹): {proposal_dir}"
        )

    proposal_dir.mkdir(parents=True, exist_ok=False)

    scorecard_dst = proposal_dir / _COPIED_SCORECARD_NAME
    observation_dst = proposal_dir / _COPIED_OBSERVATION_NAME
    shutil.copyfile(scorecard_src, scorecard_dst)
    shutil.copyfile(observation_src, observation_dst)

    source_paths = {
        "scorecard_json": str(scorecard_src),
        "observation_window_json": str(observation_src),
    }
    source_sha256 = {
        "scorecard_json": _sha256_of_file(scorecard_src),
        "observation_window_json": _sha256_of_file(observation_src),
    }

    manifest: dict[str, Any] = {
        "proposal_id": inputs.proposal_id,
        "feature": inputs.feature,
        "horizon": inputs.horizon,
        "proposer": inputs.proposer,
        "generated_at": generated_iso,
        "source_paths": source_paths,
        "source_sha256": source_sha256,
        "artifacts": {
            "manifest": _MANIFEST_NAME,
            "scorecard": _COPIED_SCORECARD_NAME,
            "observation_window_summary": _COPIED_OBSERVATION_NAME,
            "proposal_md": _PROPOSAL_MD_NAME,
        },
    }
    manifest_path = proposal_dir / _MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    proposal_md_path = proposal_dir / _PROPOSAL_MD_NAME
    proposal_md_path.write_text(
        _render_proposal_md(
            inputs,
            generated_iso=generated_iso,
            source_paths=source_paths,
            scorecard=scorecard_data,
            observation=observation_data,
        ),
        encoding="utf-8",
    )

    return ScaffoldResult(
        proposal_dir=proposal_dir,
        manifest_path=manifest_path,
        scorecard_path=scorecard_dst,
        observation_window_summary_path=observation_dst,
        proposal_md_path=proposal_md_path,
    )


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "OBSERVATION_WINDOW_REQUIRED_KEYS",
    "SCORECARD_REQUIRED_KEYS",
    "ScaffoldError",
    "ScaffoldInputs",
    "ScaffoldResult",
    "create_scaffold",
]
