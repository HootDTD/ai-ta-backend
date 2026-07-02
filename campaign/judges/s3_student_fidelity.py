"""S3 — student-graph fidelity audit (spec §4 table, row 3). THE most
important audit — this is where resolver recall/precision actually lives.

Item = per attempt, one check per node-ledger entry:

- ``credited``/``misconception`` entries: is the cited ``evidence_span``
  really where the student taught (or asserted the wrong belief about) that
  concept? A "no" here is a phantom credit.
- ``unresolved`` entries: does the FULL transcript actually contain teaching
  of that concept somewhere the resolver missed? A "yes" here is a missed
  credit (the resolver-recall gap the spec calls out by name).

Separately (pure code, NOT an LLM call — spec: "computes ledger-vs-expected
agreement as a hard code-side diff"): :func:`ledger_vs_expected` compares the
actual ledger against the persona's authored :class:`ExpectedLedger` (D2) and
reports agreement counts. This is exposed via ``JudgeResult.extra`` for E3 to
fold into the report, NOT into the LLM pass_rate (it audits a different
thing: "did the attempt land where the persona author predicted", not "is
each ledger entry individually faithful to the transcript").

Gate (E3): >=95% item-level correct.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from campaign.judges.base import JudgeResult, StageJudge, aggregate

__all__ = ["S3StudentFidelityJudge", "ledger_vs_expected"]

_SYSTEM_PROMPT = (
    "You are auditing whether a knowledge-graph node ledger entry from a "
    "student-teaches-AI session is FAITHFUL to what the student actually "
    "said. You see ONLY the full transcript of the session and ONE ledger "
    "entry (a reference concept key, the status the grader assigned it, and "
    "— for credited/misconception entries — the quoted span of the "
    "transcript the grader cites as evidence). For a credited or "
    "misconception entry: does the cited span really teach (or assert the "
    "wrong belief about) that concept? For an unresolved entry: search the "
    "WHOLE transcript — did the student actually teach that concept "
    "somewhere the grader missed? Answer ok=true when the ledger's "
    "disposition (credited/misconception/left-unresolved) matches what the "
    "transcript actually shows."
)


def ledger_vs_expected(
    ledger: list[Mapping[str, Any]], expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Pure diff between the actual ledger and the persona's authored
    expectation (D2 ``ExpectedLedger``). Returns counts + the mismatched keys
    for each of the three buckets — no LLM involved."""
    actual_credited = {
        str(e.get("key")) for e in ledger if e.get("status") in ("credited",)
    }
    actual_unresolved = {
        str(e.get("key")) for e in ledger if e.get("status") == "unresolved"
    }
    actual_misconceptions = {
        str(e.get("key")) for e in ledger if e.get("status") == "misconception"
    }

    expected_credited = set(map(str, expected.get("credited", [])))
    expected_unresolved = set(map(str, expected.get("unresolved", [])))
    expected_misconceptions = set(map(str, expected.get("misconceptions", [])))

    def _diff(actual: set[str], expected_set: set[str]) -> dict[str, Any]:
        matched = actual & expected_set
        agreement = (len(matched) / len(expected_set)) if expected_set else 1.0
        return {
            "matched": sorted(matched),
            "missing": sorted(expected_set - actual),
            "unexpected": sorted(actual - expected_set),
            "agreement": agreement,
        }

    return {
        "credited": _diff(actual_credited, expected_credited),
        "unresolved": _diff(actual_unresolved, expected_unresolved),
        "misconceptions": _diff(actual_misconceptions, expected_misconceptions),
    }


class S3StudentFidelityJudge(StageJudge):
    stage = "s3_student_fidelity"
    system_prompt = _SYSTEM_PROMPT

    def build_items(self, raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``raw`` = list of ``{attempt_id, transcript, node_ledger, expected}``
        dicts. Emits one item per ledger entry (credited, misconception, and
        unresolved statuses are all audited; any other status is skipped —
        it is not the resolver-recall/precision question this stage asks)."""
        items: list[dict[str, Any]] = []
        for attempt in raw:
            attempt_id = attempt.get("attempt_id")
            transcript = attempt.get("transcript", "")
            for entry in attempt.get("node_ledger", []):
                status = entry.get("status")
                if status not in ("credited", "misconception", "unresolved"):
                    continue
                items.append(
                    {
                        "item_id": f"{attempt_id}:{entry.get('key')}",
                        "attempt_id": attempt_id,
                        "key": entry.get("key"),
                        "status": status,
                        "evidence_span": entry.get("evidence_span"),
                        "transcript": transcript,
                    }
                )
        return items

    def user_prompt(self, item: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "key": item.get("key"),
                "status": item.get("status"),
                "evidence_span": item.get("evidence_span"),
                "transcript": item.get("transcript"),
            },
            sort_keys=True,
        )

    async def judge(self, raw: list[Mapping[str, Any]]) -> JudgeResult:
        result = await super().judge(raw)
        diffs = {
            str(attempt.get("attempt_id")): ledger_vs_expected(
                attempt.get("node_ledger", []), attempt.get("expected", {})
            )
            for attempt in raw
        }
        passed, total, pass_rate = aggregate(result.verdicts)
        return JudgeResult(
            stage=self.stage,
            verdicts=result.verdicts,
            passed=passed,
            total=total,
            pass_rate=pass_rate,
            extra={"ledger_vs_expected": diffs},
        )
