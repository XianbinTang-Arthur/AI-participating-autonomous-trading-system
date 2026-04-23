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


def _render_proposal_md(
    inputs: ScaffoldInputs,
    *,
    generated_iso: str,
    source_paths: dict[str, str],
) -> str:
    proposer = inputs.proposer or "<TBD>"
    return (
        f"# 路线 A Phase 0 · Evidence 提案 · `{inputs.feature}` @ `{inputs.horizon}`\n"
        "\n"
        "> 本文件由 `aats.cli route-a-evidence-scaffold` 生成，预填 metadata + "
        "已复制 artifact 引用。\n"
        "> 完整提案骨架请参照 "
        "`docs/research/_templates/route_a_phase0_evidence_template.md` 逐段填写。\n"
        "\n"
        "## 元数据 (提案头)\n"
        "\n"
        "| 字段 | 值 |\n"
        "|---|---|\n"
        f"| 提案 ID | `{inputs.proposal_id}` |\n"
        f"| 提案日期 | `{generated_iso}` |\n"
        f"| 提案人 | `{proposer}` |\n"
        f"| Scope: feature | `{inputs.feature}` |\n"
        f"| Scope: horizon | `{inputs.horizon}` |\n"
        "\n"
        "## 已复制 artifact\n"
        "\n"
        f"- `{_MANIFEST_NAME}` — bundle 元数据 + source provenance (sha256)\n"
        f"- `{_COPIED_SCORECARD_NAME}` — 复制自 "
        f"`{source_paths['scorecard_json']}`\n"
        f"- `{_COPIED_OBSERVATION_NAME}` — 复制自 "
        f"`{source_paths['observation_window_json']}`\n"
        "\n"
        "## 待填内容\n"
        "\n"
        "按 `docs/research/_templates/route_a_phase0_evidence_template.md` "
        "§1~§15 逐段填写。本脚手架**只**做 metadata + artifact 复制，**不**输出 "
        "verdict / go-no-go / archive。\n"
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

    _load_and_validate_json(
        scorecard_src, SCORECARD_REQUIRED_KEYS, label="scorecard"
    )
    _load_and_validate_json(
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
