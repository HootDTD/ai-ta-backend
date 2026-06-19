"""Master gate for the v1 session-personalization wedge (WU-6A3).

Mirrors ``apollo/overseer/misconception.py:is_enabled`` exactly so the two
selection call-sites (``hoot_bridge/session_init.py``, ``handlers/next.py``) and
``problem_selector.select_problem_personalized`` never duplicate the env-var
literal. Default OFF everywhere incl. prod.

Orthogonal to ``APOLLO_GRAPH_SIM_LAYER3_ENABLED`` (which gates the WRITE / table
population): personalization can be flag-ON in prod and still be a total no-op
because ``apollo_learner_state`` is empty until LAYER3 is flipped (double-gated).
"""

from __future__ import annotations

import os

__all__ = ["is_enabled"]


def is_enabled() -> bool:
    """True iff ``APOLLO_SESSION_PERSONALIZATION_ENABLED`` is truthy."""
    return os.getenv("APOLLO_SESSION_PERSONALIZATION_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
