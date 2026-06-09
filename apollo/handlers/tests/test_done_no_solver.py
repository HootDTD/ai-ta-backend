"""Diff-at-Done v1: handle_done must not touch the SymPy solver.

The full end-to-end handle_done test (test_done.py) is skipped legacy-V2 and
awaits a V3 KGGraph + Neo4j fixture rewrite (claude_v3_checklist.md item 1).
Until that lands, these module-level guards lock in the v1 contract change:
the solver is gone, coverage is awaited, and no solver_indicator escapes.
"""
import inspect

from apollo.handlers import done


def test_handle_done_is_async():
    assert inspect.iscoroutinefunction(done.handle_done)


def test_done_module_has_no_solver_symbols():
    # The SymPy solver is dropped in v1 — diff + rubric is the grade.
    assert not hasattr(done, "solve_kg_against_problem")
    assert not hasattr(done, "_format_value_text")
    assert not hasattr(done, "_serializable_trace")
    assert not hasattr(done, "_display_value")


def test_done_source_drops_solver_indicator_and_awaits_coverage():
    src = inspect.getsource(done)
    assert "solver_indicator" not in src
    assert "solve_kg_against_problem" not in src
    assert "solver_result" not in src
    # compute_coverage is now the async/parallel version and must be awaited.
    assert "await compute_coverage(" in src
