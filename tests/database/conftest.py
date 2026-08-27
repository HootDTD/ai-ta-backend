"""Shared fixtures for ``tests/database/**``.

Re-exports ``pg_committing_sessions`` (the independent-committing-AsyncSession
harness for real-Postgres concurrency tests, ``_concurrency_fixtures.py``) so
every test module under this directory can request it as an ordinary pytest
fixture without importing it directly. Importing it per test module made every
test function's ``pg_committing_sessions`` parameter look, to ruff's pyflakes
check, like a REDEFINITION of the (deliberately `# noqa: F401`-suppressed)
unused import — F811, fixture-parameter-shadows-import — because a function
parameter is not a "use" from a static-analysis standpoint. Registering the
fixture once here (a file with no fixture-consuming parameters of its own)
eliminates that false conflict for every consumer, with a single `noqa`
instead of one per test file.
"""

from __future__ import annotations

from tests.database._concurrency_fixtures import pg_committing_sessions  # noqa: F401
