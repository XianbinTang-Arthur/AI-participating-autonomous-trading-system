import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aats.data_platform.research_factory.proposals import (
    FACTOR_DSL_PROPOSAL_SCHEMA_VERSION,
    FactorDSLProposal,
    load_factor_dsl_proposal,
)

UTC = timezone.utc


def test_factor_dsl_proposal_accepts_strict_payload() -> None:
    payload = {
        "hypothesis": "Short horizon close momentum can preserve executable edge.",
        "factor_expression": " Return(close, 1) ",
        "rationale": "Recent close return is a minimal momentum signal for novelty-gated research.",
    }

    proposal = FactorDSLProposal.from_mapping(
        payload,
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    same_proposal = FactorDSLProposal.from_mapping(
        payload,
        created_at=datetime(2026, 5, 18, tzinfo=UTC),
    )

    assert proposal.schema_version == FACTOR_DSL_PROPOSAL_SCHEMA_VERSION
    assert proposal.proposal_id.startswith("factor_proposal_")
    assert proposal.proposal_id == same_proposal.proposal_id
    assert proposal.factor_expression == "Return(close, 1)"
    assert proposal.hypothesis == payload["hypothesis"]


def test_factor_dsl_proposal_rejects_extra_payload_keys() -> None:
    with pytest.raises(ValueError, match="must contain only"):
        FactorDSLProposal.from_mapping(
            {
                "hypothesis": "Volume imbalance may explain short-term moves.",
                "factor_expression": "Delta(volume, 1)",
                "rationale": "Test whether volume changes remain executable after costs.",
                "python_patch": "print('not allowed')",
            }
        )


def test_factor_dsl_proposal_rejects_invalid_factor_dsl() -> None:
    with pytest.raises(ValueError, match="attribute access"):
        FactorDSLProposal.from_mapping(
            {
                "hypothesis": "Unsafe syntax should not pass proposal validation.",
                "factor_expression": "close.__class__",
                "rationale": "Proposal-only integration must not expose Python object access.",
            }
        )


def test_factor_dsl_proposal_rejects_runtime_or_code_terms() -> None:
    with pytest.raises(ValueError, match="forbidden term: okx_write"):
        FactorDSLProposal.from_mapping(
            {
                "hypothesis": "Funding mean reversion may improve short-horizon signals.",
                "factor_expression": "Mean(funding_rate, 4)",
                "rationale": "After research, call okx_write to place orders.",
            }
        )


def test_load_factor_dsl_proposal_requires_research_artifact_path(tmp_path: Path) -> None:
    outside_path = tmp_path / "proposal.json"
    outside_path.write_text(
        json.dumps(
            {
                "hypothesis": "Close momentum may persist.",
                "factor_expression": "Return(close, 1)",
                "rationale": "Evaluate a minimal close-return factor.",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="under artifacts/research"):
        load_factor_dsl_proposal(outside_path)


def test_load_factor_dsl_proposal_from_research_artifact(tmp_path: Path) -> None:
    proposal_path = tmp_path / "artifacts" / "research" / "research_factory" / "proposals" / "proposal.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        json.dumps(
            {
                "hypothesis": "Funding drift may proxy crowded swap positioning.",
                "factor_expression": "ZScore(Mean(funding_rate, 2), 4)",
                "rationale": "Only submit a Factor DSL proposal for evidence-gated research.",
            }
        ),
        encoding="utf-8",
    )

    proposal = load_factor_dsl_proposal(
        proposal_path,
        research_root=tmp_path / "artifacts" / "research" / "research_factory" / "experiments",
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
    )

    assert proposal.factor_expression == "ZScore(Mean(funding_rate, 2), 4)"
    assert proposal.created_at == datetime(2026, 5, 17, tzinfo=UTC)
