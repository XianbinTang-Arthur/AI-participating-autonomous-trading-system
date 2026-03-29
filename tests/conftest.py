from __future__ import annotations

from tests.support.postgres import bootstrap_postgres_test_env

# Run at import time so unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), ...)
# in test modules sees the injected value before collection.
bootstrap_postgres_test_env()
