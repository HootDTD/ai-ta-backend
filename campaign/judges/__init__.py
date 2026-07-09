"""S1-S5 stage-audit judges (Plan Phase E, Task E1).

Each judge validates ONE pipeline stage's input/output (spec
``docs/superpowers/specs/2026-07-01-system-scores-outputs-design.md`` §4
philosophy: "validate stages, not vibes"). A judge sees only its own stage's
data — never the whole pipeline — and returns a :class:`~campaign.judges.base.Verdict`
per item plus a deterministic aggregated :class:`~campaign.judges.base.JudgeResult`.
Gate-bar comparison (95%/95%/95%/90%/90%-precision) is E3's job
(``campaign/report.py``); this package only produces pass rates.
"""

from campaign.judges.base import JudgeResult, StageJudge, Verdict

__all__ = ["Verdict", "JudgeResult", "StageJudge"]
