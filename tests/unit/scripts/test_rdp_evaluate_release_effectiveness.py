from __future__ import annotations

import json
from pathlib import Path

from aats.data_platform.metrics import release_effectiveness
from scripts import rdp_evaluate_release_effectiveness as cli


def _rollback_evaluation() -> dict[str, object]:
    return {
        "release_id": "rel_test",
        "family": "independent",
        "timeframe": "15m",
        "conclusion": "rollback_triggered",
        "detail": "test risk",
        "dimensions": [],
    }


def test_json_output_is_single_document_and_does_not_implicitly_enforce(
    monkeypatch,
    capsys,
) -> None:
    calls: list[bool] = []
    enforced: list[bool] = []
    monkeypatch.setattr(
        release_effectiveness,
        "evaluate_release_effectiveness",
        lambda _root, _release_id, *, save_result: (
            calls.append(save_result) or _rollback_evaluation()
        ),
    )
    monkeypatch.setattr(
        release_effectiveness,
        "enforce_pending_rollbacks",
        lambda _root: enforced.append(True) or [],
    )

    assert cli.main(["--release-id", "rel_test", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert calls == [True]
    assert enforced == []
    assert payload["risk_convergence"] == {
        "requested": False,
        "status": "not_enforced",
        "reason": "explicit --enforce is required",
    }


def test_dry_run_disables_persistence_and_enforcement(monkeypatch, capsys) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        release_effectiveness,
        "evaluate_release_effectiveness",
        lambda _root, _release_id, *, save_result: (
            calls.append(save_result) or _rollback_evaluation()
        ),
    )

    assert cli.main(["--release-id", "rel_test", "--dry-run", "--json"]) == 1

    json.loads(capsys.readouterr().out)
    assert calls == [False]


def test_enforce_is_explicit_and_embedded_in_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        release_effectiveness,
        "evaluate_release_effectiveness",
        lambda _root, _release_id, *, save_result: _rollback_evaluation(),
    )
    monkeypatch.setattr(
        release_effectiveness,
        "enforce_pending_rollbacks",
        lambda _root, *, release_ids: [
            {
                "release_id": next(iter(release_ids)),
                "ok": True,
            }
        ],
    )

    assert cli.main(["--release-id", "rel_test", "--enforce", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["risk_convergence"] == {
        "requested": True,
        "results": [{"release_id": "rel_test", "ok": True}],
    }


def test_enforcer_release_filter_skips_other_pending(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        release_effectiveness,
        "load_effectiveness_registry",
        lambda _root: {
            "evaluations": [
                {
                    "release_id": "rel_other",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                }
            ]
        },
    )

    from aats.data_platform.production_workflow import release_registry
    from aats.data_platform.production_workflow import observation_window
    from aats.data_platform.production_workflow import rollback_policy

    monkeypatch.setattr(release_registry, "load_release_history", lambda _root: {})
    monkeypatch.setattr(
        observation_window,
        "load_observation_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-target release must not be inspected")
        ),
    )
    monkeypatch.setattr(
        rollback_policy,
        "load_rollback_recommendation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-target release must not be inspected")
        ),
    )

    assert release_effectiveness.enforce_pending_rollbacks(
        Path("/unused"),
        release_ids={"rel_selected"},
    ) == []
