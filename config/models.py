"""Pinned platform model configuration (2026-07 flag reset).

``MAIN_MODEL`` / ``MAIN_REASONING_EFFORT`` used to be Railway env vars read at
~15 call sites with drifting defaults (gpt-4o here, gpt-5 there). The flag
reset hardcodes the single served value: changing a model is now a code
change + deploy, by design. Per-surface env overrides that layer ON TOP of
these constants (``APOLLO_MODEL``, ``APOLLO_CHEAP_MODEL``,
``VISION_ANSWER_MODEL``, ``APOLLO_UNIFIED_QUESTION_MODEL``, ``REPORTS_MODEL``)
are unchanged and still resolve from the environment.
"""

MAIN_MODEL: str = "gpt-5.1"
MAIN_REASONING_EFFORT: str = "low"
