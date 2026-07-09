"""Pure tests for the gate-8 dedup key (``apollo.provisioning.problem_dup_hash``).

The hash is content-only and deterministic: sha256 over a version prefix +
normalized ``problem_text`` + canonical ``given_values`` + ``target_unknown``
(spec §8B.4:1348). Course/concept scoping is the CALLER's job (the
BIGINT-concept-scoped ``existing_problem_hashes`` set), NOT this hash.

No DB, no LLM, no mocks — every test builds a real ``Problem`` and asserts on
the raw hexdigest. The three ``_differs_*`` tests plus the three
``_ignores/_treats_equal`` tests pin every component of the payload
independently, so reverting ``normalize``, ``sorted``, or any payload field
makes at least one test RED.
"""

from __future__ import annotations

from apollo.provisioning import problem_dup_hash
from apollo.schemas.problem import Problem


def _bernoulli_problem(
    *,
    problem_text: str = "Water flows through a horizontal pipe. Find P2.",
    given_values: dict | None = None,
    target_unknown: str = "P2",
    problem_id: str = "p_hash_demo",
) -> Problem:
    """Build a minimal-but-valid ``Problem`` (modeled on problem_01.json).

    ``Problem`` drops ``entity_key``/``declared_paths``, so this trims the
    annotated shape to exactly what the schema requires.
    """
    if given_values is None:
        given_values = {"A1": 0.01, "A2": 0.005, "P1": 200000.0, "v1": 2.0, "rho": 1000.0}
    return Problem.model_validate(
        {
            "id": problem_id,
            "concept_id": "bernoulli_principle",
            "difficulty": "intro",
            "problem_text": problem_text,
            "given_values": given_values,
            "target_unknown": target_unknown,
            "reference_solution": [
                {
                    "id": "continuity",
                    "step": 1,
                    "entry_type": "equation",
                    "content": {"symbolic": "rho*A1*v1 - rho*A2*v2"},
                    "depends_on": [],
                },
            ],
        }
    )


def test_hash_is_deterministic():
    p = _bernoulli_problem()
    assert problem_dup_hash(p) == problem_dup_hash(p)


def test_hash_is_sha256_hexdigest_shape():
    h = problem_dup_hash(_bernoulli_problem())
    assert len(h) == 64
    assert h == h.lower()
    int(h, 16)  # parses as hex or raises


def test_hash_ignores_problem_text_whitespace_and_case():
    a = _bernoulli_problem(problem_text="Water flows  through a pipe. Find P2.")
    b = _bernoulli_problem(
        problem_text="  water FLOWS through   a  pipe.   find p2.  ",
    )
    assert problem_dup_hash(a) == problem_dup_hash(b)


def test_hash_ignores_given_values_key_order():
    a = _bernoulli_problem(given_values={"A1": 0.01, "A2": 0.005, "P1": 200000.0})
    b = _bernoulli_problem(given_values={"P1": 200000.0, "A2": 0.005, "A1": 0.01})
    assert problem_dup_hash(a) == problem_dup_hash(b)


def test_hash_treats_float_equal_values_equal():
    a = _bernoulli_problem(given_values={"P1": 2.0})
    b = _bernoulli_problem(given_values={"P1": 2.00})
    assert problem_dup_hash(a) == problem_dup_hash(b)


def test_hash_differs_on_problem_text_change():
    a = _bernoulli_problem(problem_text="Water flows through a horizontal pipe. Find P2.")
    b = _bernoulli_problem(problem_text="Oil flows through a horizontal pipe. Find P2.")
    assert problem_dup_hash(a) != problem_dup_hash(b)


def test_hash_differs_on_given_values_change():
    a = _bernoulli_problem(given_values={"P1": 2.0})
    b = _bernoulli_problem(given_values={"P1": 3.0})
    assert problem_dup_hash(a) != problem_dup_hash(b)


def test_hash_differs_on_target_unknown_change():
    a = _bernoulli_problem(target_unknown="P2")
    b = _bernoulli_problem(target_unknown="v2")
    assert problem_dup_hash(a) != problem_dup_hash(b)


def test_dup_collision_for_semantically_identical_problems():
    """Two DISTINCT Problem objects (different id + whitespace) with the same
    normalized text + givens + target collide — the gate-8 dup case."""
    a = _bernoulli_problem(
        problem_id="problem_a",
        problem_text="Water flows through a horizontal pipe. Find P2.",
    )
    b = _bernoulli_problem(
        problem_id="problem_b",
        problem_text="  Water flows through a HORIZONTAL pipe.  Find P2. ",
    )
    assert a.id != b.id
    assert problem_dup_hash(a) == problem_dup_hash(b)
