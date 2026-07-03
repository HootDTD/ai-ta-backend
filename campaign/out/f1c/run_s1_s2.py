"""Ad-hoc F1a driver: run S1 (reference-graph) and S2 (WU-AAS ingestion)
judges against the real campaign stack, using the REAL OpenAIJudgeClient.

Not production code -- scratch glue for this one-off campaign run, per the
F1a task brief ("write ad-hoc glue code in campaign/out/f1/, not inside
campaign/judges/"). campaign/orchestrate.py does not exist yet on this
branch (Phase F1/F2 tasks are not implemented), so this script is the only
way to exercise the real S1/S2 judges end-to-end for this task.

Usage (from repo root, anaconda/base interpreter -- no torch needed):
    python campaign/out/f1/run_s1_s2.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import asyncpg  # noqa: E402

from campaign.judges.base import OpenAIJudgeClient  # noqa: E402
from campaign.judges.s1_reference_graph import S1ReferenceGraphJudge  # noqa: E402
from campaign.judges.s2_ingestion import S2IngestionJudge  # noqa: E402

PG_DSN = "postgresql://postgres:postgres@127.0.0.1:57322/postgres"

# subject_key -> concept_id (real minted concept per campaign/README.md
# provisioning tables captured earlier in this run)
SUBJECT_CONCEPTS = {
    "fluid_mechanics": [1],
    "macroeconomics": [2, 3],
    "linear_motion": [5],
}


async def _fetch_subject_graph(
    conn: asyncpg.Connection, subject_key: str, concept_ids: list[int]
) -> dict:
    nodes = []
    edges = []
    problem_texts = []
    for cid in concept_ids:
        rows = await conn.fetch(
            "SELECT id, canonical_key, kind, display_name, payload FROM apollo_kg_entities WHERE concept_id=$1",
            cid,
        )
        id_to_key = {r["id"]: r["canonical_key"] for r in rows}
        for r in rows:
            nodes.append(
                {
                    "node_id": r["canonical_key"],
                    "kind": r["kind"],
                    "display_name": r["display_name"],
                    "payload": json.loads(r["payload"])
                    if isinstance(r["payload"], str)
                    else r["payload"],
                }
            )
        prereq_rows = await conn.fetch(
            """
            SELECT p.from_entity_id, p.to_entity_id FROM apollo_entity_prereqs p
            JOIN apollo_kg_entities e ON e.id = p.from_entity_id
            WHERE e.concept_id = $1
            """,
            cid,
        )
        for pr in prereq_rows:
            fk = id_to_key.get(pr["from_entity_id"])
            tk = id_to_key.get(pr["to_entity_id"])
            if fk and tk:
                # apollo_entity_prereqs rows are generic concept->concept
                # prerequisite/dependency links, not procedure-step sequence
                # steps -- per apollo/ontology/edges.py, PRECEDES is legal
                # ONLY for (procedure_step, procedure_step) pairs. Emit
                # DEPENDS_ON so the S1 judge sees the correct edge type
                # (was mislabeled PRECEDES; see .superpowers/sdd/a3-s1-
                # adjudication.md sec 2A -- 26 false S1 failures).
                edges.append({"edge_type": "DEPENDS_ON", "from_node_id": fk, "to_node_id": tk})

        prob_rows = await conn.fetch(
            "SELECT problem_code, payload FROM apollo_concept_problems WHERE concept_id=$1", cid
        )
        for pr in prob_rows:
            payload = json.loads(pr["payload"]) if isinstance(pr["payload"], str) else pr["payload"]
            problem_texts.append(payload)

    return {
        "subject": subject_key,
        "problem": {"problems": problem_texts},
        "nodes": nodes,
        "edges": edges,
    }


async def build_s1_raw() -> list[dict]:
    conn = await asyncpg.connect(PG_DSN)
    try:
        raw = []
        for subject_key, concept_ids in SUBJECT_CONCEPTS.items():
            raw.append(await _fetch_subject_graph(conn, subject_key, concept_ids))
        return raw
    finally:
        await conn.close()


def build_s2_raw() -> list[dict]:
    """S2 items from the two REAL authored-set responses captured during
    F1c provisioning (set_id=1 promoted Problem 1(a); set_id=2 promoted
    Problem 1(b) -- see campaign/out/f1c/provisioning-notes.md; sets 3-5
    were unnecessary rejected retries, not fed to the judge)."""
    out_dir = Path(__file__).resolve().parent
    final1 = json.loads((out_dir / "authored_set_final1.json").read_text())
    final2 = json.loads((out_dir / "authored_set_final2.json").read_text())

    items = []
    for set_id, resp in ((1, final1), (2, final2)):
        for prob in resp["result_summary"]["problems"]:
            items.append(
                {
                    "item_id": f"set{set_id}:{prob['label']}",
                    "page_ref": f"authored-set {set_id} / {prob['label']}",
                    "scraped_label": prob["label"],
                    "paired_solution": {
                        "outcome": prob["outcome"],
                        "diagnostic": prob["diagnostic"],
                        "match_method": prob["match_method"],
                        "solution_source": prob["solution_source"],
                    },
                    "ocr_confidence": prob["ocr_confidence"],
                    # No low_confidence_threshold / verify_path_fired field
                    # exists anywhere in the authored-sets result_summary or
                    # apollo_ingest_runs (that table has zero rows for this
                    # run -- see provisioning-notes.md finding #3). Recorded
                    # as None so check_verify_path_fired skips these items
                    # rather than fabricating a threshold.
                    "low_confidence_threshold": None,
                    "verify_path_fired": prob.get("review_required", False),
                }
            )
    return items


def dump(result, path):
    payload = {
        "stage": result.stage,
        "passed": result.passed,
        "total": result.total,
        "pass_rate": result.pass_rate,
        "verdicts": [
            {"item_id": v.item_id, "ok": v.ok, "reason": v.reason} for v in result.verdicts
        ],
    }
    path.write_text(json.dumps(payload, indent=2))


async def main() -> None:
    llm = OpenAIJudgeClient()
    out_dir = Path(__file__).resolve().parent

    s1_raw = await build_s1_raw()
    s1 = await S1ReferenceGraphJudge(llm).judge(s1_raw)
    print(f"S1 pass_rate={s1.pass_rate:.4f} passed={s1.passed} total={s1.total}")
    dump(s1, out_dir / "s1-results.json")  # dump immediately -- don't lose S1 if S2 crashes

    s2_raw = build_s2_raw()
    s2 = await S2IngestionJudge(llm).judge(s2_raw)
    print(f"S2 pass_rate={s2.pass_rate:.4f} passed={s2.passed} total={s2.total}")
    dump(s2, out_dir / "s2-results.json")

    # per-subject S1 breakdown
    per_subject: dict[str, list] = {}
    for v in s1.verdicts:
        subj = v.item_id.split(":")[0]
        per_subject.setdefault(subj, []).append(v.ok)
    print("S1 per-subject:")
    for subj, oks in per_subject.items():
        print(f"  {subj}: {sum(oks)}/{len(oks)} = {sum(oks) / len(oks):.3f}")


if __name__ == "__main__":
    asyncio.run(main())
