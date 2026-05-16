import json
from pathlib import Path

import pytest

from aats.data_platform.research_factory.sandbox import (
    SandboxPolicy,
    SandboxProposal,
    validate_sandbox_proposal,
)
from aats.data_platform.research_factory.sandbox.guardrails import scan_candidate_patch


def test_rejects_live_execution_write_path() -> None:
    proposal = SandboxProposal(
        proposal_id="proposal_factor_1",
        proposal_type="factor",
        hypothesis="Test a research-only factor.",
        write_paths=("aats/services/execution_engine/recovery.py",),
        outputs={"candidate_factor": "Mean(close, 3)"},
    )

    with pytest.raises(ValueError, match="write path"):
        validate_sandbox_proposal(proposal, SandboxPolicy())


def test_rejects_env_read_path() -> None:
    proposal = SandboxProposal(
        proposal_id="proposal_factor_2",
        proposal_type="factor",
        hypothesis="Test a research-only factor.",
        read_paths=(".env.derivatives.live",),
        write_paths=("aats/data_platform/research_factory/sandbox/tmp/factor.py",),
        outputs={"candidate_factor": "Mean(close, 3)"},
    )

    with pytest.raises(ValueError, match="denied path"):
        validate_sandbox_proposal(proposal, SandboxPolicy())


def test_rejects_active_parameter_output() -> None:
    proposal = SandboxProposal(
        proposal_id="proposal_parameter_1",
        proposal_type="parameter",
        hypothesis="Summarize a candidate parameter recommendation.",
        write_paths=("artifacts/research/research_factory/tmp/proposal.json",),
        outputs={"active_parameter_set": {"id": "ps_unsafe"}},
    )

    with pytest.raises(ValueError, match="forbidden output term"):
        validate_sandbox_proposal(proposal, SandboxPolicy())


def test_research_factory_tmp_write_path_passes() -> None:
    proposal = SandboxProposal(
        proposal_id="proposal_factor_3",
        proposal_type="factor",
        hypothesis="Test a research-only factor.",
        read_paths=("artifacts/research/research_factory/tmp/input.json",),
        write_paths=("aats/data_platform/research_factory/sandbox/tmp/factor.py",),
        outputs={"candidate_factor": "Mean(close, 3)"},
        metadata={"source": "sandbox_contract_test"},
    )

    assert validate_sandbox_proposal(proposal, SandboxPolicy()) is proposal


def test_sandbox_policy_json_loads_with_defaults_shape() -> None:
    policy_path = Path("configs/research_factory/sandbox_policy.json")
    policy = SandboxPolicy.from_mapping(json.loads(policy_path.read_text(encoding="utf-8")))

    assert "aats/data_platform/research_factory" in policy.allowed_write_roots
    assert ".env" in policy.denied_env_patterns
    assert "active_parameter" in policy.forbidden_output_terms


def test_static_scan_rejects_os_environ_access() -> None:
    with pytest.raises(ValueError, match="forbidden module"):
        scan_candidate_patch(
            changed_paths=("aats/data_platform/research_factory/features/generated_factor.py",),
            text_blobs={
                "aats/data_platform/research_factory/features/generated_factor.py": "import os\nVALUE = os.environ\n",
            },
            policy=SandboxPolicy(),
        )


def test_static_scan_rejects_network_call_hint() -> None:
    with pytest.raises(ValueError, match="network call hint"):
        scan_candidate_patch(
            changed_paths=("aats/data_platform/research_factory/features/generated_factor.py",),
            text_blobs={
                "aats/data_platform/research_factory/features/generated_factor.py": (
                    "def run():\n"
                    "    return requests.post('https://www.okx.com')\n"
                ),
            },
            policy=SandboxPolicy(),
        )


def test_static_scan_rejects_live_execution_path() -> None:
    with pytest.raises(ValueError, match="changed path"):
        scan_candidate_patch(
            changed_paths=("aats/services/execution_engine/recovery.py",),
            text_blobs={"aats/services/execution_engine/recovery.py": "def noop():\n    return None\n"},
            policy=SandboxPolicy(),
        )


def test_static_scan_accepts_research_factory_feature_patch() -> None:
    changed_paths = scan_candidate_patch(
        changed_paths=("aats/data_platform/research_factory/features/foo.py",),
        text_blobs={
            "aats/data_platform/research_factory/features/foo.py": (
                "def compute_factor(row):\n"
                "    return row['close'] - row['open']\n"
            ),
        },
        policy=SandboxPolicy(),
    )

    assert changed_paths == ("aats/data_platform/research_factory/features/foo.py",)
