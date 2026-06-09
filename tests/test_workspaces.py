"""OBSOLETE — quarantined in Phase 0 of the testing plan (docs/TESTING-CI-PLAN.md).

These tests exercised `workspaces.build_local_static_workspace_config` and the
legacy "local static workspace" env-driven fallback (LEGACY_CLASS_* vars). That
function was removed during the pgvector / Supabase + SQLAlchemy migration —
`workspaces/__init__.py` is now empty and the symbol exists nowhere in the
codebase, so the module previously failed at *collection* and aborted the whole
suite.

Action: if local-static workspaces are reintroduced, restore real tests here.
Otherwise delete this file. Skipping (not deleting) keeps the loss of coverage
visible rather than silent.
"""

import pytest

pytest.skip(
    "obsolete: build_local_static_workspace_config removed in the pgvector/Supabase "
    "migration; workspaces now go through WorkspaceManager + DB",
    allow_module_level=True,
)
