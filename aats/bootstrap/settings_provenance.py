"""Settings Provenance Tracker — 参数来源追踪.

在 build_runtime() 合并配置的过程中，逐层记录每个参数值的变更和来源，
启动时输出摘要日志，便于排查 "这个值到底是哪层设的"。

使用方式::

    tracker = SettingsProvenanceTracker()
    tracker.snapshot("hardcoded_defaults", settings.model_dump())
    # ... merge managed profile / YAML ...
    tracker.snapshot("strategy_profile", merged_dict)
    # ... merge active parameters ...
    tracker.snapshot("active_parameters", final_dict)
    tracker.log_report()

不修改任何现有数据流，只做旁路观察。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── 只追踪策略相关字段，忽略凭证/端口等运行时字段 ─────────────────

# 前缀匹配：以这些开头的字段视为策略参数，纳入追踪
TRACKED_PREFIXES: tuple[str, ...] = (
    "strategy_",
    "active_parameters_",
    "trade_cost_",
    "ai_",
    "decision_",
)

# 精确排除：即使前缀匹配，这些字段也不追踪（敏感/噪声）
EXCLUDED_FIELDS: frozenset[str] = frozenset({
    "ai_api_key",
    "ai_api_base_url",
})


def _is_tracked(field_name: str) -> bool:
    if field_name in EXCLUDED_FIELDS:
        return False
    return any(field_name.startswith(p) for p in TRACKED_PREFIXES)


def _format_value(val: Any) -> str:
    """紧凑地格式化值，用于日志输出."""
    if val is None:
        return "None"
    if isinstance(val, bool):
        return str(val).lower()
    if isinstance(val, float):
        if not math.isfinite(val):
            return str(val)
        # 保留合理精度
        if val == int(val):
            return str(int(val))
        return f"{val:.4g}"
    if isinstance(val, (list, tuple)):
        items = ", ".join(str(x) for x in val)
        return f"[{items}]"
    return str(val)


@dataclass
class _FieldProvenance:
    """单个字段的变更历史."""
    field_name: str
    # (layer_name, value) 按时间顺序
    history: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def final_layer(self) -> str:
        return self.history[-1][0] if self.history else "unknown"

    @property
    def final_value(self) -> Any:
        return self.history[-1][1] if self.history else None

    @property
    def was_overridden(self) -> bool:
        """值是否被后续层变更过（包括 A→B→A 的情况）."""
        # history 中每条记录代表一次值变更，len>1 即发生过覆盖
        return len(self.history) > 1


class SettingsProvenanceTracker:
    """轻量级配置来源追踪器.

    在 build_runtime 的每个合并节点调用 snapshot()，
    记录哪些字段在哪一层被设置或覆盖。
    """

    # 层级名称 → 显示标签
    LAYER_LABELS: dict[str, str] = {
        "hardcoded_defaults": "① Defaults (settings.py)",
        "managed_profile":    "② Managed Profile",
        "strategy_profile":   "③ Strategy YAML",
        "env_overrides":      "④ .env Override",
        "load_settings":      "⑤ load_settings() 合并结果",
        "profile_resolution": "⑥ Profile Resolution",
        "active_parameters":  "⑦ Active Parameters (RDP)",
        "final":              "⑧ Final (validated)",
    }

    def __init__(self) -> None:
        self._snapshots: list[tuple[str, dict[str, Any]]] = []
        self._provenance: dict[str, _FieldProvenance] = {}
        self._created_at = datetime.now(timezone.utc)

    def snapshot(self, layer_name: str, settings_dict: dict[str, Any]) -> None:
        """记录一个配置层的快照.

        Parameters
        ----------
        layer_name : str
            层级标识符，如 "hardcoded_defaults", "strategy_profile", "active_parameters"
        settings_dict : dict
            该层合并后的完整 settings dict
        """
        self._snapshots.append((layer_name, dict(settings_dict)))
        self._update_provenance(layer_name, settings_dict)

    def _update_provenance(self, layer_name: str, settings_dict: dict[str, Any]) -> None:
        for field_name, value in settings_dict.items():
            if not _is_tracked(field_name):
                continue
            if field_name not in self._provenance:
                self._provenance[field_name] = _FieldProvenance(field_name=field_name)
            prov = self._provenance[field_name]
            # 只记录值变更（或首次出现）
            if not prov.history or prov.history[-1][1] != value:
                prov.history.append((layer_name, value))

    def get_overridden_fields(self) -> list[_FieldProvenance]:
        """返回被后续层覆盖过的字段（排除从未变更的）."""
        return sorted(
            [p for p in self._provenance.values() if p.was_overridden],
            key=lambda p: p.field_name,
        )

    def get_field_source(self, field_name: str) -> str | None:
        """查询某个字段的最终来源层."""
        prov = self._provenance.get(field_name)
        if prov is None:
            return None
        return prov.final_layer

    def get_field_provenance(self, field_name: str) -> _FieldProvenance | None:
        """获取单个字段的完整 provenance 记录."""
        return self._provenance.get(field_name)

    def get_all_tracked_fields(self) -> dict[str, _FieldProvenance]:
        """获取所有已追踪字段的 provenance（公共 API）."""
        return dict(self._provenance)

    def find_fields(self, pattern: str) -> list[_FieldProvenance]:
        """模糊搜索字段名（大小写不敏感）."""
        pat = pattern.lower()
        return sorted(
            [p for p in self._provenance.values() if pat in p.field_name.lower()],
            key=lambda p: p.field_name,
        )

    def build_report(self) -> dict[str, Any]:
        """构建结构化报告（可序列化为 JSON）."""
        overridden = self.get_overridden_fields()
        active_param_fields = [
            p for p in self._provenance.values()
            if any(layer == "active_parameters" for layer, _ in p.history)
        ]

        return {
            "generated_at": self._created_at.isoformat(),
            "layers_recorded": [name for name, _ in self._snapshots],
            "total_tracked_fields": len(self._provenance),
            "overridden_field_count": len(overridden),
            "active_parameter_field_count": len(active_param_fields),
            "overridden_fields": {
                p.field_name: {
                    "final_value": p.final_value,
                    "final_layer": p.final_layer,
                    "history": [
                        {"layer": layer, "value": val}
                        for layer, val in p.history
                    ],
                }
                for p in overridden
            },
            "active_parameter_injections": {
                p.field_name: {
                    "value": p.final_value,
                    "overrode_from": p.history[-2][0] if len(p.history) >= 2 else None,
                    "previous_value": p.history[-2][1] if len(p.history) >= 2 else None,
                }
                for p in active_param_fields
            },
        }

    def log_report(self) -> None:
        """输出人类可读的 provenance 摘要日志."""
        overridden = self.get_overridden_fields()
        layers = [name for name, _ in self._snapshots]

        log.info(
            "settings_provenance_summary layers=%s tracked=%d overridden=%d",
            ",".join(layers),
            len(self._provenance),
            len(overridden),
        )

        if not overridden:
            log.info("settings_provenance: 无覆盖项（所有参数均为默认值）")
            return

        # ── 按最终来源层分组输出 ─────────────────────────────
        by_layer: dict[str, list[_FieldProvenance]] = {}
        for prov in overridden:
            by_layer.setdefault(prov.final_layer, []).append(prov)

        for layer_name in layers:
            group = by_layer.get(layer_name, [])
            if not group:
                continue
            label = self.LAYER_LABELS.get(layer_name, layer_name)
            log.info("settings_provenance [%s] — %d field(s) set here:", label, len(group))
            for prov in group:
                # 显示变更链
                chain_parts = []
                for i, (lyr, val) in enumerate(prov.history):
                    lyr_short = self.LAYER_LABELS.get(lyr, lyr).split("(")[0].strip()
                    if i < len(prov.history) - 1:
                        chain_parts.append(f"{lyr_short}: {_format_value(val)}")
                    else:
                        chain_parts.append(f"→ {_format_value(val)}")
                chain_str = " | ".join(chain_parts)
                log.info("  %s  [%s]", prov.field_name, chain_str)

    def log_active_parameter_details(self) -> None:
        """专门输出 Active Parameter 注入的详细日志."""
        active_fields = [
            p for p in self._provenance.values()
            if any(layer == "active_parameters" for layer, _ in p.history)
        ]
        if not active_fields:
            log.info("settings_provenance_active_params: 无 RDP 参数注入")
            return

        log.info(
            "settings_provenance_active_params: %d field(s) injected from RDP",
            len(active_fields),
        )
        for prov in sorted(active_fields, key=lambda p: p.field_name):
            prev = prov.history[-2] if len(prov.history) >= 2 else None
            prev_str = (
                f"was {_format_value(prev[1])} from {prev[0]}"
                if prev else "new field"
            )
            log.info(
                "  [RDP→] %s = %s  (%s)",
                prov.field_name,
                _format_value(prov.final_value),
                prev_str,
            )
