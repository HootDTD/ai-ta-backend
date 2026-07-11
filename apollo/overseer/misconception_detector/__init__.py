"""Apollo misconception detector package.

Default-OFF parallel detection stage that docks or clarifies (never adds
credit) grading confidence when a student's teaching reveals a bank-known or
judge-flagged misconception. See
``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md`` for
the full design and ``docs/architecture/apollo.md`` for the reconciled,
landed contract.

This package is built incrementally task-by-task (T1-T9); re-exports are
added here as each module lands so imports never reach ahead of what exists.
"""

from __future__ import annotations

from apollo.overseer.misconception_detector.config import detector_enabled
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
    DetectorSource,
    EmbedFn,
    JudgeConceptInput,
    JudgeFn,
    JudgeRaw,
    MergeOutcome,
    Verdict,
)

__all__ = [
    "detector_enabled",
    "ConceptFinding",
    "DetectionResult",
    "DetectorSource",
    "EmbedFn",
    "JudgeConceptInput",
    "JudgeFn",
    "JudgeRaw",
    "MergeOutcome",
    "Verdict",
]
