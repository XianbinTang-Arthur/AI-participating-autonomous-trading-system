"""Resolve formal Step 3 candidate identity for downstream research rounds."""

from __future__ import annotations

import pathlib
from typing import Any

from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides

from .auto_import_candidates import load_validated_formal_step3_candidate
from .parameter_identity import parameter_values_fingerprint
from .research_artifact_contract import read_stable_json_artifact


_DEFAULT_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]


def load_parameter_candidate_lineage(
    params_json: str | pathlib.Path | None,
    *,
    project_root: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """Return formal candidate lineage, or an explicit unbound/default state.

    Legacy flat/Step 2 files remain runnable for diagnostic compatibility, but
    they are deliberately marked ``unbound`` and therefore cannot qualify a
    Step 3 parameter set for promotion.
    """

    if params_json is None:
        return {
            "status": "default",
            "source_step3_round_id": None,
            "source_step3_candidate_sha256": None,
            "combos": {},
        }
    path = pathlib.Path(params_json).absolute()
    try:
        payload, candidate_bytes = read_stable_json_artifact(
            path,
            parent=path.parent,
        )
    except ValueError as exc:
        raise ValueError("parameter_candidate_unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("parameter_candidate_shape_invalid")
    if payload.get("schema_version") != "aats.step3_candidates.v1":
        return {
            "status": "unbound",
            "source_step3_round_id": None,
            "source_step3_candidate_sha256": None,
            "combos": {},
        }

    trusted_project_root = pathlib.Path(
        project_root if project_root is not None else _DEFAULT_PROJECT_ROOT
    ).resolve()
    artifact = load_validated_formal_step3_candidate(
        trusted_project_root,
        path,
    )
    if artifact is None or artifact.metadata.get("status") != "succeeded":
        raise ValueError("parameter_candidate_formal_validation_failed")
    if artifact.candidate_bytes != candidate_bytes:
        raise ValueError("parameter_candidate_changed_during_validation")

    payload = artifact.payload
    candidates = payload["candidates"]

    combo_lineage: dict[str, Any] = {}
    for combo_key, values in candidates.items():
        family = combo_key.rsplit("_", 1)[0]
        resolved = ReplayParameterOverrides.from_dict(
            values,
            base=ReplayParameterOverrides.for_family(family),
        ).to_dict()
        combo_lineage[combo_key] = {
            "parameter_values_fingerprint": parameter_values_fingerprint(values),
            "resolved_parameter_values_fingerprint": parameter_values_fingerprint(
                resolved
            ),
        }
    return {
        "status": "bound",
        "source_step3_round_id": artifact.metadata["round_id"],
        "source_step3_candidate_sha256": artifact.candidate_sha256,
        "symbol": artifact.metadata["symbol"],
        "dataset_version": artifact.metadata["dataset_version"],
        "window": dict(artifact.metadata["window"]),
        "combos": combo_lineage,
    }
