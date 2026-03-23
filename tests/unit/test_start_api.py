from __future__ import annotations

import os
from unittest.mock import patch

from scripts.start_api import apply_runtime_bind_overrides, resolved_api_bind


def test_resolved_api_bind_honors_cli_overrides() -> None:
    with patch.dict(os.environ, {}, clear=True):
        apply_runtime_bind_overrides(host="0.0.0.0", port=8001)

        assert resolved_api_bind() == ("0.0.0.0", 8001)
