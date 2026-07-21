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
            "SELECT problem_code, difficulty, problem_text, given_values, target_unknown, "
            "reference_solution, payload_extra FROM app.problems WHERE concept_id=$1",
            cid,
        )
        for pr in prob_rows:
            extra = pr["payload_extra"]
            payload = json.loads(extra) if isinstance(extra, str) else dict(extra or {})
            solution = pr["reference_solution"]
            solution = json.loads(solution) if isinstance(solution, str) else solution
            payload.update(
                id=pr["problem_code"],
                concept_id=str(cid),
                difficulty=pr["difficulty"],
                problem_text=pr["problem_text"],
                given_values=pr["given_values"],
                target_unknown=pr["target_unknown"],
                reference_solution=(solution or {}).get("steps", []),
            )
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


async def _fetch_page_evidence(pg_dsn: str) -> dict[str, dict[str, str]]:
    """Page-level OCR evidence per ingest run (``internal.ingest_page_evidence``,
    migration 036 -- landed on staging AFTER the frozen f1/f1c runs; those
    runs had no page-level raw inputs, which is exactly the diagnosis's G4.4
    'S2 unmeasurable' finding). Returns ``{ingest_run_id: {role: ocr_text}}``.
    Empty when the table is absent (pre-036 database) so S2 degrades to the
    f1/f1c thin-input behavior instead of crashing."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        try:
            rows = await conn.fetch(
                "SELECT ingest_run_id, role, page_number, ocr_text"
                " FROM internal.ingest_page_evidence"
                " ORDER BY ingest_run_id, role, page_number"
            )
        except asyncpg.UndefinedTableError:
            return {}
        evidence: dict[str, dict[str, str]] = {}
        for r in rows:
            role_texts = evidence.setdefault(str(r["ingest_run_id"]), {})
            prior = role_texts.get(r["role"], "")
            role_texts[r["role"]] = (prior + "\n" + (r["ocr_text"] or "")).strip()
        return evidence
    finally:
        await conn.close()


async def _fetch_run_ids_by_document(pg_dsn: str, document_ids: list[int]) -> dict[int, int]:
    """Real authored-set <-> ingest-run linkage: the latest content-ingest
    row per ``problem_document_id``, mirrored from
    ``apollo/provisioning/authored_sets/api.py::_load_ingest_evidence`` (the
    same lookup the GET ``/authored-sets/{set_id}`` surface uses to expose
    ``ingest_run`` -- landed in PR #90). Returns ``{document_id: ingest_run_id}``;
    a document with no run is simply absent from the mapping.

    This replaces the old "authored set N pairs with ingest run N" positional
    assumption: PR #90 opens the ingest run BEFORE indexing, so a failed
    ingest consumes a run id without ever producing a promoted set, silently
    skewing every later index-based pairing (S2 would judge against the wrong
    page text for every set after the first failure)."""
    if not document_ids:
        return {}
    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (document_id) document_id, id
            FROM internal.content_ingest_runs
            WHERE document_id = ANY($1::bigint[])
            ORDER BY document_id, id DESC
            """,
            document_ids,
        )
        return {int(r["document_id"]): int(r["id"]) for r in rows}
    finally:
        await conn.close()


def _document_ids_from_fixtures(fixture_paths: list[Path]) -> list[int]:
    """Distinct ``problem_document_id`` values declared by each authored-set
    fixture, in file order, skipping fixtures that don't carry one (older
    fixture shape, or a set that never resolved a document)."""
    document_ids: list[int] = []
    for path in fixture_paths:
        document_id = json.loads(path.read_text(encoding="utf-8")).get("problem_document_id")
        if document_id is not None and int(document_id) not in document_ids:
            document_ids.append(int(document_id))
    return document_ids


def build_s2_raw(
    out_dir: Path,
    page_evidence: dict[str, dict[str, str]] | None = None,
    run_id_by_document: dict[int, int] | None = None,
) -> list[dict]:
    """S2 items from every ``authored_set_final*.json`` fixture found in
    ``out_dir`` (one per promoted authored set for this run). Returns an
    empty list (S2 is then skipped) if none are found.

    ``page_evidence`` (``{ingest_run_id: {role: ocr_text}}``) is attached to
    each item's ``paired_solution.source_page_ocr`` so the judge sees the
    actual scraped page text -- without it every verdict is an unmeasurable
    'insufficient info' failure (the f1/f1c pattern). Each fixture's real
    ingest run is resolved via its ``problem_document_id`` through
    ``run_id_by_document`` (see ``_fetch_run_ids_by_document``) -- NOT the
    positional "set N == run N" assumption, which breaks once a failed ingest
    consumes a run id without producing a set (see that function's docstring).
    Fixtures with no ``problem_document_id``, or no resolvable run, simply get
    no page evidence attached (graceful, matches the pre-036 thin-input
    behavior)."""
    items = []
    run_id_by_document = run_id_by_document or {}
    for final_path in sorted(out_dir.glob("authored_set_final*.json")):
        set_id = "".join(ch for ch in final_path.stem if ch.isdigit()) or final_path.stem
        resp = json.loads(final_path.read_text(encoding="utf-8"))
        document_id = resp.get("problem_document_id")
        run_id = run_id_by_document.get(int(document_id)) if document_id is not None else None
        run_evidence = (page_evidence or {}).get(str(run_id), {}) if run_id is not None else {}
        for prob in resp["result_summary"]["problems"]:
            paired_solution = {
                "outcome": prob["outcome"],
                "diagnostic": prob["diagnostic"],
                "match_method": prob["match_method"],
                "solution_source": prob["solution_source"],
            }
            if run_evidence:
                paired_solution["source_page_ocr"] = run_evidence
            items.append(
                {
                    "item_id": f"set{set_id}:{prob['label']}",
                    "page_ref": f"authored-set {set_id} / {prob['label']}",
                    "scraped_label": prob["label"],
                    "paired_solution": paired_solution,
                    "ocr_confidence": prob["ocr_confidence"],
                    # No low_confidence_threshold field exists anywhere in the
                    # authored-sets result_summary or content-ingest rows --
                    # recorded as None so check_verify_path_fired skips these
                    # items rather than fabricating a threshold.
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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def run(pg_dsn: str, out_dir: Path, subject_concepts: dict[str, list[int]]) -> None:
    llm = OpenAIJudgeClient()

    s1_raw = await build_s1_raw(pg_dsn, subject_concepts)
    s1 = await S1ReferenceGraphJudge(llm).judge(s1_raw)
    print(f"S1 pass_rate={s1.pass_rate:.4f} passed={s1.passed} total={s1.total}")
    dump(s1, out_dir / "s1-results.json")  # dump immediately -- don't lose S1 if S2 crashes

    fixture_paths = sorted(out_dir.glob("authored_set_final*.json"))
    if fixture_paths:
        document_ids = _document_ids_from_fixtures(fixture_paths)
        run_id_by_document = await _fetch_run_ids_by_document(pg_dsn, document_ids)
        page_evidence = await _fetch_page_evidence(pg_dsn)
        s2_raw = build_s2_raw(out_dir, page_evidence, run_id_by_document)
    else:
        s2_raw = []

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
