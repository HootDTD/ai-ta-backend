"""Task 9 — grading-time NLI injection behind APOLLO_NLI_ENABLED.

Tests for the ``_nli_context()`` helper and the module-global singleton in
``apollo.handlers.done_grading``.  NO real model is ever loaded — ``_build_adjudicator``
is always patched to return a lightweight sentinel.

Singleton-isolation contract
----------------------------
``_NLI_ADJUDICATOR`` is a mutable module global mutated via ``global``.
``monkeypatch.setattr`` restores module attributes, so *each* test that touches
the flag-on path resets ``_NLI_ADJUDICATOR`` to ``None`` at the start to avoid
any ordering dependency on other tests in the suite.
"""

from __future__ import annotations

import apollo.handlers.done_grading as dg
from apollo.resolution.nli_resolution import NLIContext

pytestmark = __import__("pytest").mark.unit


def test_nli_context_none_when_flag_off(monkeypatch):
    """With APOLLO_NLI_ENABLED unset, ``_nli_context()`` must return ``None``
    — grading is byte-identical to before this change."""
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    assert dg._nli_context() is None


def test_nli_context_built_when_flag_on(monkeypatch):
    """With APOLLO_NLI_ENABLED=1 and a patched builder, ``_nli_context()``
    returns a populated ``NLIContext`` whose ``.nli`` is not None."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    # Reset singleton so the builder is called fresh in THIS test.
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    sentinel = object()
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: sentinel)
    ctx = dg._nli_context()
    assert isinstance(ctx, NLIContext)
    assert ctx.nli is sentinel


def test_nli_context_reuses_singleton(monkeypatch):
    """Calling ``_nli_context()`` twice with the flag on reuses the SAME
    adjudicator object — the builder must be called exactly once.

    This test covers the ``if _NLI_ADJUDICATOR is None:`` False branch,
    which is otherwise unreachable by the two tests above.
    """
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    # Reset so we start from a known-empty state.
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)

    call_count = 0

    def counting_builder():
        nonlocal call_count
        call_count += 1
        return object()

    monkeypatch.setattr(dg, "_build_adjudicator", counting_builder)

    ctx1 = dg._nli_context()
    ctx2 = dg._nli_context()

    # Builder called only once (singleton reuse).
    assert call_count == 1
    # Both calls return a context whose .nli is the SAME instance.
    assert ctx1 is not None and ctx2 is not None
    assert ctx1.nli is ctx2.nli
