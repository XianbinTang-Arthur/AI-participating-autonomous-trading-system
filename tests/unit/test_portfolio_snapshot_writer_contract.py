from __future__ import annotations

from pathlib import Path


def test_production_services_do_not_directly_save_portfolio_snapshots() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    services_root = repo_root / "aats" / "services"
    allowed = {
        services_root / "portfolio_service" / "snapshot_writer.py",
    }
    offenders: list[str] = []
    for path in services_root.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "portfolio_repo.save_snapshot(" in text:
            offenders.append(str(path.relative_to(repo_root)))

    assert offenders == []
