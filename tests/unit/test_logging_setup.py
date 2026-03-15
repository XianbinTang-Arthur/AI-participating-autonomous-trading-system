from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from aats.bootstrap.logging import configure_logging, get_logger


class LoggingSetupTests(unittest.TestCase):
    def test_configure_logging_creates_directories_and_writes_level_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            configure_logging(
                "DEBUG",
                log_dir=temp_dir,
                rotate_max_bytes=4096,
                backup_count=2,
            )
            logger = get_logger("aats.test.logging")
            logger.debug("debug-message")
            logger.info("info-message")
            logger.warning("warning-message")
            logger.error("error-message")

            for handler in logging.getLogger().handlers:
                handler.flush()

            base_path = Path(temp_dir)
            expected_directories = (
                base_path / "runtime",
                base_path / "debug",
                base_path / "info",
                base_path / "warning",
                base_path / "error",
            )
            for directory in expected_directories:
                self.assertTrue(directory.exists(), directory)

            runtime_log = (base_path / "runtime" / "aats.log").read_text(encoding="utf-8")
            debug_log = (base_path / "debug" / "debug.log").read_text(encoding="utf-8")
            info_log = (base_path / "info" / "info.log").read_text(encoding="utf-8")
            warning_log = (base_path / "warning" / "warning.log").read_text(encoding="utf-8")
            error_log = (base_path / "error" / "error.log").read_text(encoding="utf-8")

            self.assertIn("debug-message", runtime_log)
            self.assertIn("info-message", runtime_log)
            self.assertIn("warning-message", runtime_log)
            self.assertIn("error-message", runtime_log)
            self.assertIn("debug-message", debug_log)
            self.assertIn("info-message", info_log)
            self.assertIn("warning-message", warning_log)
            self.assertIn("error-message", error_log)

            root_logger = logging.getLogger()
            for handler in list(root_logger.handlers):
                root_logger.removeHandler(handler)
                handler.close()
            logging.shutdown()
