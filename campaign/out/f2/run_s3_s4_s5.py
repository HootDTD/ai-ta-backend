"""F1b ad-hoc driver: run the S3 (student-graph fidelity), S4 (Apollo
coherence) and S5 (misconception precision) judges LIVE (real
OpenAIJudgeClient) over the AttemptRecords in campaign/out/f2/attempts.jsonl.

Not production code -- scratch glue per the F1a/F1b precedent
(campaign/out/f1/run_s1_s2.py); campaign/orchestrate.py does not exist on
this branch.

Judge inputs are assembled through the REAL adapters (campaign/adapters.py):
  - S3: attempt_to_s3_item  (one per OK attempt, canonical artifact ledger)
  - S4: attempt_to_s4_item  (one per OK attempt -- session-level)
  - S5: attempt_to_s5_item  (asserted misconceptions vs bank)

Payload selection (discovered live, first pass of this script): in this
shadow-mode tuning run the CANONICAL (llm_fallback) artifact carries an
EMPTY node_ledger / misconceptions / clarification_trace -- the whole
per-node ledger surface lives on the PAIR (graph) payload. S3 ("student-
GRAPH fidelity"), S4 (clarification trace) and S5 (asserted misconceptions)
all audit exactly that ledger, so items are built from
campaign.adapters.graph_payload_for(...) (falling back to canonical only if
a graph payload is missing). A first pass built them from canonical and got
S3 total=0 / S5 total=0 -- vacuous, not a pass; kept here as the honest
record of why this selection exists.

Usage: python -m campaign.out.f1.run_s3_s4_s5
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402


def _load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env(REPO_ROOT / ".env.campaign")

from campaign.adapters import (  # noqa: E402
    attempt_to_s3_item,
    attempt_to_s4_item,
    attempt_to_s5_item,
    graph_payload_for,
)
from campaign.cast.personas.schema import ExpectedLedger  # noqa: E402
from campaign.judges.base import OpenAIJudgeClient, load_jsonl  # noqa: E402
from campaign.judges.s3_student_fidelity import S3StudentFidelityJudge  # noqa: E402
from campaign.judges.s4_apollo_coherence import S4ApolloCoherenceJudge  # noqa: E402
from campaign.judges.s5_misconceptions import S5MisconceptionJudge  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent


def dump(result, path: Path) -> None:
    payload = {
        "stage": result.stage,
        "passed": result.passed,
        "total": result.total,
        "pass_rate": result.pass_rate,
        "verdicts": [
            {"item_id": v.item_id, "ok": v.ok, "reason": v.reason} for v in result.verdicts
        ],
        "extra": json.loads(json.dumps(dict(result.extra), default=str)),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_inputs(attempts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    s3_raw: list[dict] = []
    s4_raw: list[dict] = []
    s5_raw: list[dict] = []
    for att in attempts:
        if att.get("status") != "ok" or not att.get("artifact_canonical"):
            continue
        expected = ExpectedLedger(
            credited=att["expected"].get("credited", []),
            unresolved=att["expected"].get("unresolved", []),
            misconceptions=att["expected"].get("misconceptions", []),
            expects_clarification=att["expected"].get("expects_clarification", False),
        )
        artifact = (
            graph_payload_for(
                artifact_canonical=att.get("artifact_canonical"),
                artifact_pair=att.get("artifact_pair"),
            )
            or att["artifact_canonical"]
        )
        s3_raw.append(
            attempt_to_s3_item(
                attempt_id=att["attempt_id"],
                transcript=att["transcript"],
                artifact=artifact,
                expected=expected,
            )
        )
        s4_raw.append(
            attempt_to_s4_item(
                attempt_id=att["attempt_id"],
                transcript=att["transcript"],
                artifact=artifact,
            )
        )
        s5_raw.append(
            attempt_to_s5_item(
                attempt_id=att["attempt_id"],
                artifact=artifact,
                expected=expected,
                subject=att["subject"],
                concept=att["concept"],
            )
        )
    return s3_raw, s4_raw, s5_raw


async def main() -> None:
    attempts = load_jsonl(OUT_DIR / "attempts.jsonl")
    print(f"loaded {len(attempts)} attempt records")
    s3_raw, s4_raw, s5_raw = build_inputs(attempts)
    print(f"S3 attempts={len(s3_raw)} S4 sessions={len(s4_raw)} S5 attempts={len(s5_raw)}")

    llm = OpenAIJudgeClient()

    s3 = await S3StudentFidelityJudge(llm).judge(s3_raw)
    print(f"S3 pass_rate={s3.pass_rate:.4f} passed={s3.passed} total={s3.total}")
    dump(s3, OUT_DIR / "s3-results.json")

    s4 = await S4ApolloCoherenceJudge(llm).judge(s4_raw)
    print(f"S4 pass_rate={s4.pass_rate:.4f} passed={s4.passed} total={s4.total}")
    dump(s4, OUT_DIR / "s4-results.json")

    s5 = await S5MisconceptionJudge(llm).judge(s5_raw)
    print(f"S5 pass_rate={s5.pass_rate:.4f} passed={s5.passed} total={s5.total}")
    dump(s5, OUT_DIR / "s5-results.json")


if __name__ == "__main__":
    asyncio.run(main())
