"""S2 — WU-AAS ingestion audit (spec §4 table, row 2).

Item = one ``(pdf page reference, scraped label, paired solution)`` triple
recorded by the teacher cast (D1) during a real authored-set upload, plus the
``IngestRun`` confidence flag for that item. Checks: scraped labels match the
PDF page, problem<->solution pairing is correct, OCR text is faithful to the
page image, and — as a CODE-side check (no LLM needed, it's a boolean
read-back) — the low-confidence -> verify path fired exactly when the ingest
run flagged it.

Gate (E3): >=95% item-level correct.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from campaign.judges.base import JudgeResult, StageJudge, Verdict, aggregate

__all__ = ["S2IngestionJudge", "check_verify_path_fired"]

_SYSTEM_PROMPT = (
    "You are auditing one page of a real WU-AAS (authored-set) ingestion run. "
    "You see ONLY: a reference to the source PDF page, the label the scraper "
    "extracted for it, and the problem/solution pairing the pipeline "
    "produced from OCR text. Judge whether the scraped label truly matches "
    "the page, whether the problem-solution pairing is correct, and whether "
    "the OCR text is faithful to what the page actually says. Do not assume "
    "anything beyond what is given."
)


def check_verify_path_fired(items: list[Mapping[str, Any]]) -> list[Verdict]:
    """Pure code-side check: every item whose recorded ``ocr_confidence`` is
    below its ``low_confidence_threshold`` must have ``verify_path_fired``
    true, and vice versa (a verify path firing on a confident item is also a
    defect — it means the threshold read is wrong). Returns one
    :class:`Verdict` per item (not gated on ``ok`` — reported alongside the
    LLM verdicts by E3, same shape so it folds into the same pass_rate)."""
    verdicts: list[Verdict] = []
    for item in items:
        item_id = str(item.get("item_id", ""))
        confidence = item.get("ocr_confidence")
        threshold = item.get("low_confidence_threshold")
        fired = bool(item.get("verify_path_fired", False))
        if confidence is None or threshold is None:
            continue
        should_fire = confidence < threshold
        if should_fire == fired:
            verdicts.append(
                Verdict(item_id=f"{item_id}:verify_path", ok=True, reason="verify path fired as expected")
            )
        else:
            verdicts.append(
                Verdict(
                    item_id=f"{item_id}:verify_path",
                    ok=False,
                    reason=(
                        f"ocr_confidence={confidence} threshold={threshold} "
                        f"should_fire={should_fire} but verify_path_fired={fired}"
                    ),
                )
            )
    return verdicts


class S2IngestionJudge(StageJudge):
    stage = "s2_ingestion"
    system_prompt = _SYSTEM_PROMPT

    def build_items(self, raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``raw`` = list of ``{item_id, page_ref, scraped_label,
        paired_solution, ocr_confidence, low_confidence_threshold,
        verify_path_fired}`` dicts, one per audited page/problem triple."""
        return [dict(item) for item in raw]

    def item_id(self, item: Mapping[str, Any]) -> str:
        return str(item.get("item_id", ""))

    def user_prompt(self, item: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "page_ref": item.get("page_ref"),
                "scraped_label": item.get("scraped_label"),
                "paired_solution": item.get("paired_solution"),
            },
            sort_keys=True,
        )

    async def judge(self, raw: Any) -> JudgeResult:
        result = await super().judge(raw)
        verify_verdicts = check_verify_path_fired(self.build_items(raw))
        all_verdicts = list(result.verdicts) + verify_verdicts
        passed, total, pass_rate = aggregate(all_verdicts)
        return JudgeResult(
            stage=self.stage,
            verdicts=tuple(all_verdicts),
            passed=passed,
            total=total,
            pass_rate=pass_rate,
        )
