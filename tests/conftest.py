"""
Bootstrap the test environment before any app module is imported.

1. Set DATABASE_URL so core.py does not raise at import time.
2. Stub native / heavy dependencies (asyncpg, transformers, torch).
3. Stub the AsyncPG OTel instrumentor so it does not try to wrap the mocked
   asyncpg module via wrapt (which fails because MagicMock is not a real package).

Why stubs instead of skipping the imports?
  core.py runs `import asyncpg` at the top of the file.  That line raises
  ModuleNotFoundError if the package is absent — even when no test ever calls
  real asyncpg code.  Every test that would reach actual asyncpg / transformers
  logic already replaces the relevant function (init_db, get_db_pool,
  classify_symptoms, get_classifier …) with an AsyncMock or MagicMock *before*
  that code path executes.  The stubs below satisfy the import statements so
  the module loads cleanly; they are placeholder objects, never actually called.
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@localhost:5432/testdb")

for _mod in ("asyncpg", "transformers", "torch"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# The real opentelemetry-instrumentation-asyncpg package (when installed) calls
# wrapt.wrap_function_wrapper("asyncpg.connection", ...) during instrument().
# That import path fails because our asyncpg stub above is a plain MagicMock, not
# a real package with sub-modules.  Override the instrumentor module so telemetry.py
# gets a no-op stub instead of the real one.
sys.modules["opentelemetry.instrumentation.asyncpg"] = MagicMock()
