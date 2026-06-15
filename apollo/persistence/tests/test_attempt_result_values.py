"""Drift guard: the app-layer result allowlist must equal the migration's.

The Done-grading 500 (staging, 2026-06-15) was a migration (009) and the
handler code drifting apart with nothing asserting they agreed: handle_done
wrote ``result='graded'`` while the CHECK constraint only allowed
``('solved','stuck','skipped','returned_to_hoot')``. handle_next's
``'abandoned'`` had the same latent defect.

Real-Postgres behaviour of the constraint is covered by
``tests/database/test_apollo_attempt_result_constraint.py`` (applies the actual
migration SQL on Testcontainers). This fast unit test guards the OTHER half:
that ``apollo.persistence.models.ATTEMPT_RESULTS`` — the allowlist app code
reasons about (e.g. ``GRADED_ATTEMPT_RESULTS`` in re-attempt detection) — is
exactly the set the migration permits. If a future change widens one without
the other, this fails before the code can reach a database.
"""

from __future__ import annotations

import re
from pathlib import Path

from apollo.persistence.models import ATTEMPT_RESULTS, GRADED_ATTEMPT_RESULTS

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "database" / "migrations"
_RESULT_MIGRATION = _MIGRATIONS_DIR / "025_apollo_attempt_result_values.sql"


def _migration_allowlist() -> set[str]:
    """Values inside the ADD CONSTRAINT ... `result IN ( ... )` of migration 025."""
    sql = _RESULT_MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"result\s+IN\s*\(([^)]*)\)", sql, re.IGNORECASE)
    assert match, "could not find `result IN (...)` in migration 025"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_migration_allowlist_matches_model_constant():
    assert _migration_allowlist() == set(ATTEMPT_RESULTS), (
        "migration 025 and models.ATTEMPT_RESULTS disagree: "
        f"migration={sorted(_migration_allowlist())} model={sorted(ATTEMPT_RESULTS)}"
    )


def test_graded_results_are_subset_of_allowed():
    # The values that count as a prior grade must all be DB-legal, and the only
    # value intentionally excluded from the graded set is 'abandoned'.
    assert set(GRADED_ATTEMPT_RESULTS) < set(ATTEMPT_RESULTS)
    assert set(ATTEMPT_RESULTS) - set(GRADED_ATTEMPT_RESULTS) == {"abandoned"}


def test_constraint_name_is_replaced():
    # Migration 025 must DROP/ADD the same constraint name migration 009 created,
    # otherwise the old (narrower) constraint would linger alongside the new one.
    sql = _RESULT_MIGRATION.read_text(encoding="utf-8")
    assert sql.count("apollo_problem_attempts_result_check") >= 2  # DROP + ADD
