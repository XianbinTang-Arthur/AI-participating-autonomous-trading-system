#!/usr/bin/env python3
"""Invalidate historical capital eligibility and create deterministic v2 plans."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any, Mapping

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.research_factory.validation.candidate_replay import (  # noqa: E402
    audit_historical_candidate,
    build_candidate_v2_replay_plan,
)


def _load_existing_audit_ids(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    result: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, Mapping) or not payload.get("audit_id"):
                raise ValueError(f"invalid_registry_line:{line_number}")
            result.add(str(payload["audit_id"]))
    return result


def _append_jsonl(path: pathlib.Path, payloads: list[Mapping[str, Any]]) -> None:
    if not payloads:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def _write_plan_once(path: pathlib.Path, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"existing_plan_content_mismatch:{path.name}")
        return "existing"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
    return "created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment-root",
        type=pathlib.Path,
        default=pathlib.Path("artifacts/research/research_factory/experiments"),
    )
    parser.add_argument(
        "--registry",
        type=pathlib.Path,
        default=pathlib.Path(
            "artifacts/research/research_factory/artifact_eligibility_registry.jsonl"
        ),
    )
    parser.add_argument(
        "--plan-root",
        type=pathlib.Path,
        default=pathlib.Path("artifacts/research/research_factory/replay_plans"),
    )
    args = parser.parse_args(argv)

    experiment_root = args.experiment_root.resolve()
    artifact_root = experiment_root.parent.resolve()
    existing_ids = _load_existing_audit_ids(args.registry)
    new_audits: list[dict[str, Any]] = []
    plans_created = 0
    plans_existing = 0
    timestamp = datetime.now(UTC)
    candidate_paths = sorted(experiment_root.glob("*/candidate_artifact.json"))
    if not candidate_paths:
        raise ValueError("historical_candidates_required")
    for candidate_path in candidate_paths:
        spec_path = candidate_path.parent / "experiment_spec.json"
        if not spec_path.is_file():
            raise ValueError(f"experiment_spec_missing:{candidate_path.parent.name}")
        audit = audit_historical_candidate(
            candidate_path,
            artifact_root=artifact_root,
            evaluated_at=timestamp,
        )
        if audit.audit_id not in existing_ids:
            new_audits.append(audit.to_dict())
            existing_ids.add(audit.audit_id)
        plan = build_candidate_v2_replay_plan(
            audit=audit,
            candidate_path=candidate_path,
            experiment_spec_path=spec_path,
            artifact_root=artifact_root,
            created_at=timestamp,
        )
        state = _write_plan_once(args.plan_root / f"{plan.plan_id}.json", plan.to_dict())
        if state == "created":
            plans_created += 1
        else:
            plans_existing += 1
    _append_jsonl(args.registry, new_audits)
    print(
        json.dumps(
            {
                "candidate_count": len(candidate_paths),
                "capital_eligible_count": 0,
                "new_audit_rows": len(new_audits),
                "plans_created": plans_created,
                "plans_existing": plans_existing,
                "registry": args.registry.as_posix(),
                "plan_root": args.plan_root.as_posix(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
