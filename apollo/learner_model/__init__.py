"""WU-5A1 — the PURE 3-state Bayesian filter core (§3 / §6.5 math), no IO.

The frozen public surface WU-5A2 (persistence + Done wiring) imports. Mirrors the
``apollo/grading/__init__.py`` re-export convention. This package is PURE: NO DB,
NO LLM, NO Neo4j, NO containers — see ``belief.py`` / ``update.py`` /
``state_model.py`` module headers.
"""

from __future__ import annotations

from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    CORRECTED_LIKELIHOOD,
    GAMMA,
    LIKELIHOOD_FLOOR,
    MISCONCEPTION_FLAG_THRESHOLD,
    MISCONCEPTION_LIKELIHOOD,
    MISSING_LIKELIHOOD,
    NO_OP_LIKELIHOOD,
    bayes_update,
    confidence_of,
    damp,
    likelihood_for_event,
    mastery_of,
    misconception_code_of,
)
from apollo.learner_model.state_model import (
    BeliefUpdate,
    LearnerStateRowSpec,
    MasteryEventRowSpec,
)
from apollo.learner_model.update import apply_event, event_to_row_specs

__all__ = [
    # belief.py — LOCKED constants
    "COLD_START_PRIOR",
    "GAMMA",
    "LIKELIHOOD_FLOOR",
    "MISCONCEPTION_FLAG_THRESHOLD",
    "MISSING_LIKELIHOOD",
    "MISCONCEPTION_LIKELIHOOD",
    "CORRECTED_LIKELIHOOD",
    "NO_OP_LIKELIHOOD",
    # belief.py — the 6 math fns
    "likelihood_for_event",
    "damp",
    "bayes_update",
    "mastery_of",
    "confidence_of",
    "misconception_code_of",
    # update.py — orchestration
    "apply_event",
    "event_to_row_specs",
    # state_model.py — frozen value objects
    "BeliefUpdate",
    "MasteryEventRowSpec",
    "LearnerStateRowSpec",
]
