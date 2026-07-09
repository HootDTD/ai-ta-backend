"""WU-4A1 Task-2 — the ``symbolic_mappings`` DB round-trip gate (real Postgres).

Decision 4 of the plan: the per-problem ``symbolic_mappings`` key (added to
``problem_01.json`` on disk) must survive BOTH seeders into
``apollo_concept_problems.payload`` with ZERO seeder code change. The registry
seeder reads the problem JSON verbatim into the payload, and the learner-model
seeder passes that payload through ``annotate_reference_solution`` which does a
shallow ``dict(problem)`` (preserving every existing key) and only ADDS
``entity_key`` / ``declared_paths`` / ``layer1_seeded``.

This test reuses the WU-3B real-Postgres harness verbatim
(``tests/database/test_seed_apollo_learner_model.py``): a per-test fresh pgvector
DB built from the content-scoped migration chain + 026, with ``auth.users`` /
``aita_search_spaces`` stubs. The bernoulli curriculum is seeded by inserting the
on-disk problem payloads directly (exactly what the registry seeder does:
``json.loads(p.read_text())`` into the payload column), then the learner-model
``seed`` runs with ``write_disk=False`` so the test never rewrites the on-disk
JSON. We then assert ``payload["symbolic_mappings"] == {"d": "2*r"}`` survived.

DB gate (CLAUDE.md): this is a LOCAL Docker pgvector container test (the project
DB gate fires here), never a remote Supabase project. No migration is applied to
any remote project.
"""

from __future__ import annotations

import pytest

from scripts.seed_apollo_learner_model import seed
from tests.database.test_seed_apollo_learner_model import (  # reuse the WU-3B harness
    _fetchval,
    _seed_one_course,
    seeded_db,  # noqa: F401 - re-exported fixture
)

pytestmark = pytest.mark.integration


async def test_symbolic_mappings_round_trips_into_concept_problem_payload(seeded_db):  # noqa: F811 - pytest injects the re-exported fixture
    """The additive ``symbolic_mappings`` key survives the registry-equivalent
    disk-verbatim load AND the learner-model seed (write_disk=False)."""
    sa_dsn, plain = await seeded_db(_seed_one_course)

    # The learner-model seed reads the payloads the curriculum insert wrote
    # (disk verbatim) and re-writes them via annotate_reference_solution. Run it
    # with write_disk=False so the on-disk JSON is never rewritten by the test.
    await seed(sa_dsn, write_disk=False)

    payload = await _fetchval(
        plain,
        "SELECT payload FROM apollo_concept_problems WHERE problem_code = $1",
        "bernoulli_horizontal_pipe_find_p2",
    )
    # asyncpg returns JSONB as a JSON string; the WU-3B harness decodes with
    # json.loads. Be tolerant of both a decoded dict and a raw JSON string.
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)

    assert payload["symbolic_mappings"] == {"d": "2*r"}
    # The learner seed's annotation must still have run (proves we exercised the
    # full both-seeders path, not just the raw insert).
    assert payload["layer1_seeded"] is True
    assert payload.get("declared_paths")
