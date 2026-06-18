"""WU-5A1 — package-seam + PURE-contract pins.

Locks the FROZEN surface WU-5A2 imports from ``apollo.learner_model`` (every name
importable from the package root) and asserts the unit imports NO IO
(sqlalchemy/asyncpg/openai/neo4j) — the PURE contract. Mirrors
``apollo/grading/tests/test_package_seam.py``.
"""

from __future__ import annotations

import inspect

import apollo.learner_model.belief as belief_mod
import apollo.learner_model.state_model as state_mod
import apollo.learner_model.update as update_mod
from apollo.learner_model import (
    CORRECTED_LIKELIHOOD,
    NO_OP_LIKELIHOOD,
    BeliefUpdate,
    COLD_START_PRIOR,
    GAMMA,
    LIKELIHOOD_FLOOR,
    LearnerStateRowSpec,
    MISCONCEPTION_FLAG_THRESHOLD,
    MISCONCEPTION_LIKELIHOOD,
    MISSING_LIKELIHOOD,
    MasteryEventRowSpec,
    apply_event,
    bayes_update,
    confidence_of,
    damp,
    event_to_row_specs,
    likelihood_for_event,
    mastery_of,
    misconception_code_of,
)

_IO_TOKENS = ("sqlalchemy", "asyncpg", "openai", "neo4j")


def test_public_api_importable_from_package_root():
    import apollo.learner_model as lm

    expected = {
        "COLD_START_PRIOR",
        "GAMMA",
        "LIKELIHOOD_FLOOR",
        "MISCONCEPTION_FLAG_THRESHOLD",
        "MISSING_LIKELIHOOD",
        "MISCONCEPTION_LIKELIHOOD",
        "CORRECTED_LIKELIHOOD",
        "NO_OP_LIKELIHOOD",
        "likelihood_for_event",
        "damp",
        "bayes_update",
        "mastery_of",
        "confidence_of",
        "misconception_code_of",
        "apply_event",
        "event_to_row_specs",
        "BeliefUpdate",
        "MasteryEventRowSpec",
        "LearnerStateRowSpec",
    }
    assert expected == set(lm.__all__)
    for name in expected:
        assert hasattr(lm, name), f"apollo.learner_model is missing {name}"

    # Callables are callable; constants/classes are defined.
    for fn in (
        likelihood_for_event,
        damp,
        bayes_update,
        mastery_of,
        confidence_of,
        misconception_code_of,
        apply_event,
        event_to_row_specs,
    ):
        assert callable(fn)
    for cls in (BeliefUpdate, MasteryEventRowSpec, LearnerStateRowSpec):
        assert isinstance(cls, type)
    assert COLD_START_PRIOR == (0.20, 0.60, 0.20)
    assert GAMMA == 1.5
    assert LIKELIHOOD_FLOOR == 0.02
    assert MISCONCEPTION_FLAG_THRESHOLD == 0.5
    assert MISSING_LIKELIHOOD == (0.7, 1.0, 0.4)
    assert MISCONCEPTION_LIKELIHOOD == (3.0, 1.0, 0.2)
    assert CORRECTED_LIKELIHOOD == (0.5, 1.5, 1.2)
    assert NO_OP_LIKELIHOOD == (1.0, 1.0, 1.0)


def test_no_io_imports():
    """The PURE contract: none of the three source modules reference an IO token
    (DB/LLM/Neo4j) in their source."""
    for mod in (belief_mod, state_mod, update_mod):
        src = inspect.getsource(mod)
        for token in _IO_TOKENS:
            assert token not in src, f"{mod.__name__} must not reference {token!r}"
