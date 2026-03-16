from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from aats.bootstrap.settings import AATSSettings


class TestAATSSettings(unittest.TestCase):
    def test_model_validate_dict_ignores_process_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AATS_MODE": "guarded_live",
                "AATS_MAX_NOTIONAL_PER_SYMBOL": "20",
                "AATS_LIVE_SUBMIT_ENABLED": "true",
            },
            clear=False,
        ):
            settings = AATSSettings.model_validate({"execution_backend": "paper"})

        self.assertEqual(settings.mode, "paper_live")
        self.assertEqual(settings.max_notional_per_symbol, 1_000.0)
        self.assertFalse(settings.live_submit_enabled)
        self.assertEqual(settings.execution_backend, "paper")


if __name__ == "__main__":
    unittest.main()
