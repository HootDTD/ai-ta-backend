"""Manual retrieval smoke test.

This module is intentionally skipped in automated CI/local pytest runs because
it requires a live database and external embedding API access.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(
    reason="Manual smoke test only (requires live SUPABASE_DB_URL + OpenAI network access)."
)


def test_retrieval_manual_smoke_placeholder() -> None:
    """Placeholder so pytest reports this module as a deliberate skip."""
    assert True
