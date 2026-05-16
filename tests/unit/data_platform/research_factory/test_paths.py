import json
from pathlib import Path

import pytest

from aats.data_platform.research_factory.paths import copy_research_artifact_file


def test_copy_research_artifact_file_rejects_destination_outside_research_root(
    tmp_path: Path,
) -> None:
    research_root = tmp_path / "artifacts" / "research"
    source = research_root / "phase4" / "execution_cost_summary.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"ok": True}), encoding="utf-8")
    outside_destination = tmp_path / "private"

    with pytest.raises(ValueError, match="under artifacts/research"):
        copy_research_artifact_file(
            source,
            outside_destination,
            destination_name="execution_cost_summary.json",
            research_root=research_root,
        )


def test_copy_research_artifact_file_copies_inside_research_root(tmp_path: Path) -> None:
    research_root = tmp_path / "artifacts" / "research"
    source = research_root / "phase4" / "execution_cost_summary.json"
    destination_dir = research_root / "research_factory" / "experiments" / "exp_1"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"ok": True}), encoding="utf-8")

    ref = copy_research_artifact_file(
        source,
        destination_dir,
        destination_name="execution_cost_summary.json",
        research_root=research_root,
    )

    assert ref == "execution_cost_summary.json"
    assert json.loads((destination_dir / ref).read_text(encoding="utf-8")) == {"ok": True}
