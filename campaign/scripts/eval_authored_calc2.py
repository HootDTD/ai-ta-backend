"""Authored calc-2 eval driver (reversed provisioning acceptance).

Drives the 6 HW problem/solution PDF pairs from
``apollo/provisioning/corpora/calc2/authored/`` through the REAL teacher
upload path (``POST /apollo/authored-sets``), then scores the outcome against
the corpus ground truth and the committed gold graphs:

  * ``match_report.json`` — concept-match accuracy vs the corpus's private
    ``concept_slug`` (bar: >= 0.95), NO_MATCH/held/rejected tallies;
  * ``graph_diff_report.json`` — per-problem structural diff vs the committed
    ``apollo/subjects/calculus_2`` graphs (bar: zero opaque ids), node/edge
    coverage aggregates.

Held problems are deliberately NOT auto-approved: the acceptance bar measures
the pipeline's OWN automatic outcomes.

Two subcommands:

  bootstrap  — mint the campaign teacher + a fresh course (SearchSpace +
               teacher CourseMembership); writes <out-dir>/course.json.
               Needs SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / --pg-dsn.
               Seed the premade list AFTER this, BEFORE `run`:
                 python -m scripts.seed_premade_concepts --database-url <dsn> \\
                   --search-space-id <id> --subject-slug calculus_2 \\
                   --concepts-json apollo/provisioning/corpora/calc2/concepts.json \\
                   --vocab-from-subject calculus_2
  run        — upload the 6 pairs, poll to terminal, dump + score + diff.

LOCAL CAMPAIGN STACK ONLY (127.0.0.1) — never point this at staging/prod.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from campaign.scripts.diff_generated_vs_authored import (  # noqa: E402
    align_problems,
    diff_graph,
    norm_slug,
    score_concept_match,
)

_CORPUS_DIR = REPO_ROOT / "apollo" / "provisioning" / "corpora" / "calc2" / "authored"
_GOLD_ROOT = REPO_ROOT / "apollo" / "subjects" / "calculus_2" / "concepts"

TEACHER_EMAIL = "revgen-teacher@example.com"
TEACHER_PASSWORD = "RevgenTeacher123!"


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #


async def _mint_teacher(supabase_url: str, service_role_key: str) -> tuple[str, str]:
    """Create (idempotent) the teacher auth user; return (user_id, token).

    The JWT's own "sub" claim is the only trustworthy user-id source (the
    admin list-users lookup returned foreign ids on this GoTrue version —
    see campaign/out/b0smoke/bootstrap_course.py)."""
    async with httpx.AsyncClient(base_url=supabase_url, timeout=30.0) as client:
        headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        await client.post(
            "/auth/v1/admin/users",
            json={"email": TEACHER_EMAIL, "password": TEACHER_PASSWORD, "email_confirm": True},
            headers=headers,
        )
        signin = await client.post(
            "/auth/v1/token?grant_type=password",
            json={"email": TEACHER_EMAIL, "password": TEACHER_PASSWORD},
            headers={"apikey": service_role_key},
        )
        signin.raise_for_status()
        token = signin.json()["access_token"]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        user_id = json.loads(base64.urlsafe_b64decode(payload_b64))["sub"]
        return user_id, token


async def _bootstrap(args: argparse.Namespace) -> None:
    supabase_url = os.environ["SUPABASE_URL"]
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    teacher_id, teacher_token = await _mint_teacher(supabase_url, service_role_key)

    engine = create_async_engine(args.pg_dsn)
    async with engine.begin() as conn:
        space_id = (
            await conn.execute(
                text(
                    "INSERT INTO aita_search_spaces (name, slug, subject_name, created_at, "
                    "updated_at) VALUES (:n, :s, :subj, now(), now()) RETURNING id"
                ),
                {"n": "Revgen Calc2", "s": f"revgen-calc2-{int(time.time())}", "subj": "Calc 2"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO course_memberships (user_id, search_space_id, role, "
                "created_at, updated_at) VALUES (:u, :ss, 'teacher', now(), now())"
            ),
            {"u": teacher_id, "ss": space_id},
        )
    await engine.dispose()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    course = {
        "search_space_id": int(space_id),
        "teacher_user_id": teacher_id,
        "teacher_token": teacher_token,
    }
    (out_dir / "course.json").write_text(json.dumps(course, indent=2))
    print(json.dumps({"search_space_id": int(space_id)}, indent=2))
    print(
        "\nNext: seed the premade list, then `run`:\n"
        f"  .venv/bin/python -m scripts.seed_premade_concepts --database-url {args.pg_dsn} "
        f"--search-space-id {space_id} --subject-slug calculus_2 "
        "--concepts-json apollo/provisioning/corpora/calc2/concepts.json "
        "--vocab-from-subject calculus_2"
    )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


async def _upload_and_poll(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    token: str,
    search_space_id: int,
    problem_pdf: Path,
    solution_pdf: Path,
    poll_interval: float,
    poll_timeout: float,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    with problem_pdf.open("rb") as pf, solution_pdf.open("rb") as sf:
        resp = await client.post(
            f"{base_url}/apollo/authored-sets",
            headers=headers,
            files={
                "problem": (problem_pdf.name, pf, "application/pdf"),
                "solution": (solution_pdf.name, sf, "application/pdf"),
            },
            data={"search_space_id": str(search_space_id)},
        )
    resp.raise_for_status()
    set_id = int(resp.json()["set_id"])

    deadline = time.monotonic() + poll_timeout
    while True:
        status_resp = await client.get(f"{base_url}/apollo/authored-sets/{set_id}", headers=headers)
        status_resp.raise_for_status()
        row = status_resp.json()
        if row.get("status") in ("done", "failed"):
            return row
        if time.monotonic() > deadline:
            raise TimeoutError(f"set {set_id} did not reach a terminal status")
        await asyncio.sleep(poll_interval)


async def _dump_generated(pg_dsn: str, search_space_id: int) -> list[dict]:
    engine = create_async_engine(pg_dsn)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT cp.id, cp.tier, cp.payload, cp.provenance, cp.solution_source, "
                    "c.slug AS concept_slug, c.id AS concept_db_id "
                    "FROM apollo_concept_problems cp "
                    "JOIN apollo_concepts c ON cp.concept_id = c.id "
                    "JOIN apollo_subjects s ON c.subject_id = s.id "
                    "WHERE s.search_space_id = :ss"
                ),
                {"ss": search_space_id},
            )
        ).mappings()
        out = []
        for r in rows:
            payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
            prov = (
                r["provenance"]
                if isinstance(r["provenance"], dict | type(None))
                else json.loads(r["provenance"])
            )
            out.append(
                {
                    "concept_problem_id": int(r["id"]),
                    "tier": int(r["tier"]),
                    "payload": payload,
                    "provenance": prov or {},
                    "solution_source": r["solution_source"],
                    "concept_slug": str(r["concept_slug"]),
                    "concept_db_id": int(r["concept_db_id"]),
                }
            )
    await engine.dispose()
    return out


def _classify_generated(rows: list[dict]) -> list[dict]:
    """One eval record per generated problem: outcome + effective concept slug
    (None for a NO_MATCH hold; matched slug for other holds when recorded)."""
    records: list[dict] = []
    for row in rows:
        review = (row["provenance"] or {}).get("authored_review") or {}
        if row["tier"] == 2:
            outcome = "promoted"
            slug: str | None = row["concept_slug"]
        elif review.get("required"):
            outcome = "held_for_review"
            if review.get("reason") == "no_matching_concept":
                slug = None
            else:
                slug = (review.get("concept_match") or {}).get("slug") or None
        else:
            outcome = "tier1_unpromoted"
            slug = None
        if norm_slug(str(slug or "")) == "provisional_inventory":
            slug = None
        records.append({**row, "outcome": outcome, "concept_slug": slug})
    return records


def _load_gold_index() -> list[dict]:
    gold: list[dict] = []
    for path in sorted(_GOLD_ROOT.glob("*/problems/problem_*.json")):
        problem = json.loads(path.read_text())
        problem["_concept_dir"] = path.parent.parent.name
        problem["_path"] = str(path.relative_to(REPO_ROOT))
        gold.append(problem)
    return gold


def _match_gold(generated_payload: dict, gold: list[dict]) -> dict | None:
    from campaign.scripts.diff_generated_vs_authored import text_jaccard

    text_g = str(generated_payload.get("problem_text") or "")
    best, best_score = None, 0.0
    for g in gold:
        score = text_jaccard(text_g, str(g.get("problem_text") or ""))
        if score > best_score:
            best, best_score = g, score
    return best if best_score >= 0.6 else None


async def _run(args: argparse.Namespace) -> None:
    course = json.loads(Path(args.course_json).read_text())
    search_space_id = int(args.search_space_id or course["search_space_id"])
    token = str(course["teacher_token"])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    set_rows: list[dict] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for hw in range(1, 7):
            problem_pdf = _CORPUS_DIR / f"hw{hw}_problem.pdf"
            solution_pdf = _CORPUS_DIR / f"hw{hw}_solution.pdf"
            print(f"uploading HW{hw} ...", flush=True)
            row = await _upload_and_poll(
                client,
                base_url=args.base_url,
                token=token,
                search_space_id=search_space_id,
                problem_pdf=problem_pdf,
                solution_pdf=solution_pdf,
                poll_interval=args.poll_interval,
                poll_timeout=args.poll_timeout,
            )
            print(f"  HW{hw}: status={row.get('status')}")
            set_rows.append({"hw": hw, **row})
    (out_dir / "set_rows.json").write_text(json.dumps(set_rows, indent=2, default=str))

    corpus = json.loads((_CORPUS_DIR / "authored_corpus.json").read_text())
    generated = _classify_generated(await _dump_generated(args.pg_dsn, search_space_id))
    (out_dir / "generated_problems.json").write_text(json.dumps(generated, indent=2, default=str))

    aligned = align_problems(generated, corpus)
    match_report = score_concept_match(aligned)
    outcome_counts: dict[str, int] = {}
    for g in generated:
        outcome_counts[g["outcome"]] = outcome_counts.get(g["outcome"], 0) + 1
    match_report["outcome_counts"] = outcome_counts
    (out_dir / "match_report.json").write_text(json.dumps(match_report, indent=2))

    gold = _load_gold_index()
    diffs: list[dict] = []
    opaque_total = 0
    for gen, entry in aligned:
        if gen["outcome"] != "promoted":
            continue
        committed = _match_gold(gen["payload"], gold)
        diff = diff_graph(gen["payload"], committed)
        diff["concept_problem_id"] = gen["concept_problem_id"]
        diff["corpus_problem_id"] = entry.get("problem_id") if entry else None
        diff["gold_path"] = committed.get("_path") if committed else None
        opaque_total += len(diff["opaque_ids"])
        diffs.append(diff)
    graph_report = {
        "promoted_graphs": len(diffs),
        "opaque_id_total": opaque_total,
        "diffs": diffs,
    }
    (out_dir / "graph_diff_report.json").write_text(json.dumps(graph_report, indent=2))

    concept_ids = sorted({g["concept_db_id"] for g in generated if g["outcome"] == "promoted"})
    print(
        json.dumps(
            {
                "match_accuracy": match_report["accuracy"],
                "outcomes": outcome_counts,
                "opaque_id_total": opaque_total,
                "promoted_graphs": len(diffs),
            },
            indent=2,
        )
    )
    print(
        "\nNext (S1 judge; needs OPENAI_API_KEY):\n"
        f"  .venv/bin/python -m campaign.scripts.run_s1_s2 --out-dir {out_dir} "
        f"--pg-dsn {args.s1_pg_dsn or args.pg_dsn.replace('+asyncpg', '')} "
        f"--subjects calculus_2:{','.join(str(c) for c in concept_ids)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="mint teacher + course; write course.json")
    b.add_argument("--pg-dsn", required=True, help="postgresql+asyncpg:// local campaign DSN")
    b.add_argument("--out-dir", required=True)

    r = sub.add_parser("run", help="upload the 6 HW pairs and score")
    r.add_argument("--base-url", default="http://127.0.0.1:8000")
    r.add_argument("--pg-dsn", required=True, help="postgresql+asyncpg:// local campaign DSN")
    r.add_argument("--s1-pg-dsn", default=None, help="plain postgresql:// DSN for run_s1_s2")
    r.add_argument("--course-json", required=True)
    r.add_argument("--search-space-id", type=int, default=None)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--poll-interval", type=float, default=3.0)
    r.add_argument("--poll-timeout", type=float, default=1800.0)

    args = parser.parse_args()
    if args.cmd == "bootstrap":
        asyncio.run(_bootstrap(args))
    else:
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
