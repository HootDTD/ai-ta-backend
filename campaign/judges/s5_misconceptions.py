"""S5 — misconception-assertion audit (spec §4 table, row 5). Precision-
focused: each misconception the grader asserted must really be wrong.

Item = one asserted misconception: the triggering student utterance + the
misconception-bank entry it was matched against. Gate (E3): precision >=90%
(``ok`` = "yes, this really is the wrong belief the bank entry describes").
Recall against the persona's authored ``expected.misconceptions`` (D2) is
computed as a pure code-side diff and reported via ``JudgeResult.extra`` —
NOT gated (spec: "recall reported, not gated").
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from campaign.judges.base import JudgeResult, StageJudge, aggregate

__all__ = ["S5MisconceptionJudge", "misconception_recall"]

_SYSTEM_PROMPT = (
    "You are auditing one misconception the grader asserted a student holds, "
    "during a student-teaches-AI session. You see ONLY the student's "
    "triggering utterance and the misconception-bank entry (its description "
    "of the wrong belief) the grader matched it against. Judge ok=true only "
    "if the utterance really does display that specific wrong belief — not "
    "just an unclear or incomplete statement, and not a DIFFERENT "
    "misconception than the one named."
)


def misconception_recall(attempts: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure code-side recall check: for each attempt, which of the persona's
    ``expected.misconceptions`` keys were actually asserted by the grader.
    Reported, not gated (spec explicitly defers misconception recall
    targets)."""
    per_attempt: dict[str, Any] = {}
    total_expected = 0
    total_found = 0
    for attempt in attempts:
        attempt_id = str(attempt.get("attempt_id"))
        expected = set(map(str, attempt.get("expected", {}).get("misconceptions", [])))
        asserted = {str(m.get("key")) for m in attempt.get("asserted_misconceptions", [])}
        found = expected & asserted
        missed = expected - asserted
        total_expected += len(expected)
        total_found += len(found)
        per_attempt[attempt_id] = {
            "found": sorted(found),
            "missed": sorted(missed),
            "recall": (len(found) / len(expected)) if expected else 1.0,
        }
    overall_recall = (total_found / total_expected) if total_expected else 1.0
    return {"per_attempt": per_attempt, "overall_recall": overall_recall}


class S5MisconceptionJudge(StageJudge):
    stage = "s5_misconceptions"
    system_prompt = _SYSTEM_PROMPT

    def build_items(self, raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``raw`` = list of ``{attempt_id, expected, asserted_misconceptions}``
        dicts where ``asserted_misconceptions`` is a list of
        ``{key, utterance, bank_description}``. One item per asserted
        misconception (across all attempts)."""
        items: list[dict[str, Any]] = []
        for attempt in raw:
            attempt_id = attempt.get("attempt_id")
            for misconception in attempt.get("asserted_misconceptions", []):
                items.append(
                    {
                        "item_id": f"{attempt_id}:{misconception.get('key')}",
                        "utterance": misconception.get("utterance"),
                        "bank_description": misconception.get("bank_description"),
                    }
                )
        return items

    def user_prompt(self, item: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "utterance": item.get("utterance"),
                "bank_description": item.get("bank_description"),
            },
            sort_keys=True,
        )

    async def judge(self, raw: list[Mapping[str, Any]]) -> JudgeResult:
        result = await super().judge(raw)
        passed, total, pass_rate = aggregate(result.verdicts)
        return JudgeResult(
            stage=self.stage,
            verdicts=result.verdicts,
            passed=passed,
            total=total,
            pass_rate=pass_rate,
            extra={"recall": misconception_recall(raw)},
        )
