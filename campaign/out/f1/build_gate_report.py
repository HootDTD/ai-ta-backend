"""F1b ad-hoc driver: build the tuning-phase gate report (GATE-REPORT.md +
scoreboard.json) from the run's real artifacts in campaign/out/f1/:

  - s1-results.json / s2-results.json  (F1a, real judge runs)
  - s3-results.json / s4-results.json / s5-results.json (run_s3_s4_s5.py)
  - attempts.jsonl                     (run_f1b_corpus.py, live corpus)
  - config.json                        (frozen CampaignConfig snapshot)
  - adjudication-verdicts.jsonl        (optional -- Fable adjudication is a
    SEPARATE downstream step; when absent the adjudication gate correctly
    reads 0 packets = failing, per report.py's no-vacuous-pass rule)

Usage: python -m campaign.out.f1.build_gate_report
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from campaign.adapters import all_subject_kinds, attempt_to_report_record  # noqa: E402
from campaign.config import load_frozen  # noqa: E402
from campaign.judges.base import JudgeResult, Verdict, load_jsonl  # noqa: E402
from campaign.report import build_report, write_report  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
RUN_ID = "f1"


def load_judge_result(path: Path) -> JudgeResult | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    verdicts = tuple(
        Verdict(item_id=v["item_id"], ok=v["ok"], reason=v["reason"])
        for v in data.get("verdicts", [])
    )
    return JudgeResult(
        stage=data["stage"],
        verdicts=verdicts,
        passed=data["passed"],
        total=data["total"],
        pass_rate=data["pass_rate"],
        extra=data.get("extra", {}),
    )


def main() -> None:
    config, sha = load_frozen(OUT_DIR / "config.json")

    judge_results = {}
    for stem in ("s1-results", "s2-results", "s3-results", "s4-results", "s5-results"):
        result = load_judge_result(OUT_DIR / f"{stem}.json")
        if result is not None:
            judge_results[result.stage] = result

    raw_attempts = load_jsonl(OUT_DIR / "attempts.jsonl")
    attempts = [
        attempt_to_report_record(
            attempt_id=a.get("attempt_id"),
            subject=a.get("subject", ""),
            artifact_canonical=a.get("artifact_canonical"),
            artifact_pair=a.get("artifact_pair"),
        )
        for a in raw_attempts
    ]

    adjudication = load_jsonl(OUT_DIR / "adjudication-verdicts.jsonl")

    report = build_report(
        run_id=RUN_ID,
        config=config,
        config_sha=sha,
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=all_subject_kinds(),
    )
    md_path, json_path = write_report(report, OUT_DIR)
    print(f"wrote {md_path} and {json_path}")
    print(f"overall: {'PASS' if report.passed else 'FAIL'}")
    for g in report.gates:
        print(f"  [{'PASS' if g.passed else 'FAIL'}] {g.detail}")


if __name__ == "__main__":
    main()
