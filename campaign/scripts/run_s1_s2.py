"""Canonical S1/S2 judge driver -- run S1 (reference-graph) and S2 (WU-AAS
ingestion) judges against the real campaign stack, using the REAL
``OpenAIJudgeClient``.

This is the CANONICAL template for every NEXT campaign run. The historical,
frozen copies under ``campaign/out/f1/run_s1_s2.py`` and
``campaign/out/f1c/run_s1_s2.py`` are per-run artifacts that must stay
byte-faithful to the ``s1-results.json``/``s2-results.json`` they already
produced (see ``campaign/README.md``) -- do NOT edit them for future fixes.
Land future S1/S2 harness changes HERE and invoke this module for new runs:

    python -m campaign.scripts.run_s1_s2 \
        --out-dir campaign/out/<new_run_id> \
        --subjects fluid_mechanics:1 macroeconomics:2,3 linear_motion:5

``--out-dir`` must already contain the run's authored-set S2 fixtures
(``authored_set_final*.json``, one per promoted set) if S2 is to run;
S2 is skipped with a warning if none are found.

Edge-type emission: ``apollo_entity_prereqs`` rows are generic
concept->concept prerequisite/dependency links, not procedure-step sequence
steps -- per ``apollo/ontology/edges.py``, PRECEDES is legal ONLY for
``(procedure_step, procedure_step)`` pairs. This driver emits DEPENDS_ON for
every prereq row (see ``.superpowers/sdd/a3-s1-adjudication.md`` sec 2A --
the PRECEDES mislabel drove 26 of 57 recorded S1 failures in the f1/f1c
frozen runs, before this fix).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncpg  # noqa: E402

from campaign.judges.base import OpenAIJudgeClient  # noqa: E402
from campaign.judges.s1_reference_graph import S1ReferenceGraphJudge  # noqa: E402
from campaign.judges.s2_ingestion import S2IngestionJudge  # noqa: E402

DEFAULT_PG_DSN = "postgresql://postgres:postgres@127.0.0.1:57322/postgres"


def parse_subjects(specs: list[str]) -> dict[str, list[int]]:
    """Parse ``subject_key:cid[,cid...]`` specs into ``{subject_key: [cid, ...]}``."""
    subjects: dict[str, list[int]] = {}
    for spec in specs:
        key, _, cids = spec.partition(":")
        subjects[key] = [int(c) for c in cids.split(",") if c]
    return subjects


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
                # (see .superpowers/sdd/a3-s1-adjudication.md sec 2A, and
                # docs/_archive/experiments/2026-07-03-s1-judge-adjudication.md
                # in this worktree for the committed copy).
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


async def build_s1_raw(pg_dsn: str, subject_concepts: dict[str, list[int]]) -> list[dict]:
    conn = await asyncpg.connect(pg_dsn)
    try:
        raw = []
        for subject_key, concept_ids in subject_concepts.items():
            raw.append(await _fetch_subject_graph(conn, subject_key, concept_ids))
        return raw
    finally:
        await conn.close()


def build_s2_raw(out_dir: Path) -> list[dict]:
    """S2 items from every ``authored_set_final*.json`` fixture found in
    ``out_dir`` (one per promoted authored set for this run). Returns an
    empty list (S2 is then skipped) if none are found."""
    items = []
    for final_path in sorted(out_dir.glob("authored_set_final*.json")):
        set_id = "".join(ch for ch in final_path.stem if ch.isdigit()) or final_path.stem
        resp = json.loads(final_path.read_text())
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
                    # apollo_ingest_runs -- recorded as None so
                    # check_verify_path_fired skips these items rather than
                    # fabricating a threshold.
                    "low_confidence_threshold": None,
                    "verify_path_fired": prob.get("review_required", False),
                }
            )
    return items


def dump(result, path: Path) -> None:
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


async def run(pg_dsn: str, out_dir: Path, subject_concepts: dict[str, list[int]]) -> None:
    llm = OpenAIJudgeClient()

    s1_raw = await build_s1_raw(pg_dsn, subject_concepts)
    s1 = await S1ReferenceGraphJudge(llm).judge(s1_raw)
    print(f"S1 pass_rate={s1.pass_rate:.4f} passed={s1.passed} total={s1.total}")
    dump(s1, out_dir / "s1-results.json")  # dump immediately -- don't lose S1 if S2 crashes

    s2_raw = build_s2_raw(out_dir)
    if s2_raw:
        s2 = await S2IngestionJudge(llm).judge(s2_raw)
        print(f"S2 pass_rate={s2.pass_rate:.4f} passed={s2.passed} total={s2.total}")
        dump(s2, out_dir / "s2-results.json")
    else:
        print(f"S2 skipped: no authored_set_final*.json fixtures found under {out_dir}")

    per_subject: dict[str, list] = {}
    for v in s1.verdicts:
        subj = v.item_id.split(":")[0]
        per_subject.setdefault(subj, []).append(v.ok)
    print("S1 per-subject:")
    for subj, oks in per_subject.items():
        print(f"  {subj}: {sum(oks)}/{len(oks)} = {sum(oks) / len(oks):.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--pg-dsn", default=DEFAULT_PG_DSN)
    parser.add_argument(
        "--subjects",
        nargs="+",
        required=True,
        metavar="subject_key:cid[,cid...]",
        help="e.g. fluid_mechanics:1 macroeconomics:2,3",
    )
    args = parser.parse_args()
    subject_concepts = parse_subjects(args.subjects)
    asyncio.run(run(args.pg_dsn, args.out_dir, subject_concepts))


if __name__ == "__main__":
    main()
