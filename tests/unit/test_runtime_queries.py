from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.services.operator.runtime_queries import RuntimeQueryFacade


class _FakeOwner:
    def __init__(
        self,
        *,
        phase5_enabled: bool,
        financial_convergence_mode_enabled: bool,
        portfolio_ledger_truth_enabled: bool,
    ) -> None:
        self._phase5_enabled = phase5_enabled
        self.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                financial_convergence_mode_enabled=financial_convergence_mode_enabled,
                portfolio_ledger_truth_enabled=portfolio_ledger_truth_enabled,
            )
        )

    def _phase5_control_plane_enabled(self) -> bool:
        return self._phase5_enabled


class TestRuntimeQueryFacade(unittest.TestCase):
    def test_control_plane_consistency_marks_phase5_without_financial_convergence_as_transitional(self) -> None:
        facade = RuntimeQueryFacade(
            _FakeOwner(
                phase5_enabled=True,
                financial_convergence_mode_enabled=False,
                portfolio_ledger_truth_enabled=True,
            )
        )

        snapshot = facade._control_plane_consistency()

        self.assertEqual(snapshot["status"], "transitional")
        self.assertIn(
            "phase5_control_plane_running_without_financial_convergence",
            snapshot["warning_codes"],
        )

    def test_control_plane_consistency_marks_ledger_truth_without_phase5_as_transitional(self) -> None:
        facade = RuntimeQueryFacade(
            _FakeOwner(
                phase5_enabled=False,
                financial_convergence_mode_enabled=False,
                portfolio_ledger_truth_enabled=True,
            )
        )

        snapshot = facade._control_plane_consistency()

        self.assertEqual(snapshot["status"], "transitional")
        self.assertIn(
            "portfolio_ledger_truth_enabled_without_phase5_control_plane",
            snapshot["warning_codes"],
        )
