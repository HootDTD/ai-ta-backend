"""Re-exports the real-Neo4j Testcontainers fixture for apollo/emergent/tests.

``tc_neo4j`` is defined in ``apollo/knowledge_graph/tests/conftest.py``.
conftest fixtures are only visible down their OWN directory tree, so
``apollo/emergent/tests`` (a sibling tree) re-imports it here rather than
declaring ``pytest_plugins`` (which double-registers the module when the
full ``apollo/`` suite collects both directories in one run). Mirrors
``apollo/conftest.py``'s own re-export of ``tests.conftest``'s ``_pg_url`` /
``db_session``.
"""

from __future__ import annotations

from apollo.knowledge_graph.tests.conftest import _tc_neo4j_conn, tc_neo4j  # noqa: F401
