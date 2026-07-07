"""Resolver V2 — inverted-loop node/edge recovery (flagged shadow engine).

Design: ``docs/_archive/specs/2026-07-07-resolver-v2-design.md``. A parallel
scoring engine behind ``APOLLO_RESOLVER_V2`` (default OFF) that substitutes
exactly three ``GradeResult`` numbers; the v1 resolver is never touched.

``resolver_v2_enabled`` is exported eagerly (tiny, no heavy deps — safe to
import at ``done_grading.py`` module top). ``run_resolver_v2`` is exported
LAZILY via module ``__getattr__`` so importing this package never pulls the
engine (and, transitively, transformers) at import time.
"""

from __future__ import annotations

from apollo.resolver_v2.config import resolver_v2_enabled

__all__ = ["resolver_v2_enabled", "run_resolver_v2"]


def __getattr__(name: str):
    """Lazy re-export of the T7 engine entrypoint."""
    if name == "run_resolver_v2":
        from apollo.resolver_v2.engine import run_resolver_v2

        return run_resolver_v2
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
