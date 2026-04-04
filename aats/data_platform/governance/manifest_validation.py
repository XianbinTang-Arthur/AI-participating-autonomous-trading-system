"""Manifest 规范校验.

校验 round_manifest.json 是否符合 Phase 5 统一规范。
支持所有 phase 的 manifest：Step 1/2, Phase 3, Phase 4。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any


# ── 统一 manifest 必须字段 ──────────────────────────────────────────

REQUIRED_FIELDS: list[str] = [
    "round_id",
    "started_at",
    "finished_at",
    "status",
    "scope",
]

SCOPE_FIELDS: list[str] = ["family", "symbol", "timeframe"]

RECOMMENDED_FIELDS: list[str] = [
    "input_refs",
    "output_refs",
    "code_version",
    "notes",
]

VALID_STATUSES: set[str] = {
    "pending",
    "running",
    "succeeded",
    "partial_success",
    "failed",
    "deprecated",
}

# Phase 标识
KNOWN_PHASES: set[str] = {
    "phase2_step1",
    "phase2_step2",
    "phase3",
    "phase4",
}


# ── 校验结果 ────────────────────────────────────────────────────────


@dataclass
class ValidationIssue:
    """单条校验问题."""

    level: str  # "error" | "warning" | "info"
    field: str
    message: str


@dataclass
class ManifestValidationResult:
    """一个 manifest 的校验结果."""

    path: str
    round_id: str | None = None
    phase: str | None = None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "round_id": self.round_id,
            "phase": self.phase,
            "is_valid": self.is_valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [
                {"level": i.level, "field": i.field, "message": i.message}
                for i in self.issues
            ],
        }


# ── 校验逻辑 ────────────────────────────────────────────────────────


def validate_manifest(manifest: dict[str, Any], *, path: str = "") -> ManifestValidationResult:
    """校验单个 manifest dict 是否符合规范."""
    result = ManifestValidationResult(path=path)
    result.round_id = manifest.get("round_id")
    result.phase = manifest.get("phase")

    # 1. 必须字段
    for f in REQUIRED_FIELDS:
        if f not in manifest or manifest[f] is None:
            result.issues.append(ValidationIssue("error", f, f"缺少必须字段: {f}"))

    # 2. status 合法性
    status = manifest.get("status")
    if status and status not in VALID_STATUSES:
        result.issues.append(
            ValidationIssue("error", "status", f"非法 status: {status}, 合法值: {sorted(VALID_STATUSES)}")
        )

    # 3. scope
    scope = manifest.get("scope")
    if isinstance(scope, dict):
        for sf in SCOPE_FIELDS:
            if sf not in scope:
                result.issues.append(
                    ValidationIssue("warning", f"scope.{sf}", f"scope 缺少推荐字段: {sf}")
                )
    elif scope is not None:
        result.issues.append(
            ValidationIssue("error", "scope", "scope 应为 dict 类型")
        )

    # 4. phase 标识
    phase = manifest.get("phase")
    if phase and phase not in KNOWN_PHASES:
        result.issues.append(
            ValidationIssue("warning", "phase", f"未知 phase: {phase}")
        )

    # 5. 推荐字段
    for f in RECOMMENDED_FIELDS:
        if f not in manifest:
            result.issues.append(
                ValidationIssue("info", f, f"缺少推荐字段: {f}")
            )

    # 6. 时间戳格式
    for ts_field in ("started_at", "finished_at"):
        val = manifest.get(ts_field)
        if val and not isinstance(val, str):
            result.issues.append(
                ValidationIssue("warning", ts_field, f"{ts_field} 应为 ISO 格式字符串")
            )

    # 7. input_refs
    input_refs = manifest.get("input_refs")
    if isinstance(input_refs, dict):
        if "dataset_version" not in input_refs:
            result.issues.append(
                ValidationIssue("info", "input_refs.dataset_version", "缺少 dataset_version")
            )
    elif input_refs is not None:
        result.issues.append(
            ValidationIssue("warning", "input_refs", "input_refs 应为 dict 类型")
        )

    return result


def validate_manifest_file(path: pathlib.Path) -> ManifestValidationResult:
    """读取并校验一个 round_manifest.json 文件."""
    str_path = str(path)
    if not path.exists():
        result = ManifestValidationResult(path=str_path)
        result.issues.append(
            ValidationIssue("error", "file", f"文件不存在: {str_path}")
        )
        return result

    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        result = ManifestValidationResult(path=str_path)
        result.issues.append(
            ValidationIssue("error", "file", f"无法解析 JSON: {exc}")
        )
        return result

    return validate_manifest(data, path=str_path)


def normalize_legacy_manifest(manifest: dict[str, Any], *, phase: str) -> dict[str, Any]:
    """将旧版 manifest 补全到 Phase 5 统一规范.

    不修改原 dict，返回新 dict。
    """
    out = dict(manifest)

    # phase
    if "phase" not in out:
        out["phase"] = phase

    # status — 旧 manifest 可能没有顶级 status
    if "status" not in out:
        combos = out.get("combos", [])
        if combos:
            statuses = {c.get("status") for c in combos}
            if statuses == {"succeeded"}:
                out["status"] = "succeeded"
            elif "failed" in statuses and statuses - {"failed"} == set():
                out["status"] = "failed"
            elif "failed" in statuses or "partial_success" in statuses:
                out["status"] = "partial_success"
            else:
                out["status"] = "succeeded"
        else:
            out["status"] = "succeeded"

    # scope — 旧 manifest 可能直接存 symbol 而非 scope 结构
    if "scope" not in out:
        scope: dict[str, Any] = {}
        if "symbol" in out:
            scope["symbol"] = out["symbol"]
        if "window" in out:
            scope["window"] = out["window"]
        # combos 里的 family/timeframe 信息
        combos = out.get("combos", [])
        if combos:
            families = sorted({c.get("family", "") for c in combos if c.get("family")})
            timeframes = sorted({c.get("timeframe", "") for c in combos if c.get("timeframe")})
            if families:
                scope["families"] = families
            if timeframes:
                scope["timeframes"] = timeframes
        out["scope"] = scope

    # input_refs
    if "input_refs" not in out:
        refs: dict[str, Any] = {}
        if "dataset_version" in out:
            refs["dataset_version"] = out["dataset_version"]
        out["input_refs"] = refs if refs else None

    # output_refs
    if "output_refs" not in out:
        out["output_refs"] = None

    # code_version / notes
    out.setdefault("code_version", None)
    out.setdefault("notes", None)

    return out
