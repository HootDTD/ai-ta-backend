"""Manual full /ask endpoint smoke test.

Kept as an opt-in placeholder for local exploratory runs.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(
    reason="Manual smoke test only (requires running backend server + external services)."
)


def test_full_pipeline_manual_smoke_placeholder() -> None:
    """Placeholder so pytest reports this module as a deliberate skip."""
    assert True
