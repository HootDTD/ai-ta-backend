"""apollo_grade_probe.py — reusable local driver for the Apollo graph-grading path.

PURPOSE
    Test how the Neo4j knowledge-graph grading ("graph RAG") behaves on real
    (mock) problems + real student explanations, WITHOUT the broken auto-
    provisioning LLM generation. Hand-authored problems are seeded as tier-2 by
    the three seed scripts (run those first); for the macro probe:

        python -m scripts.seed_apollo_concept_registry
        python scripts/seed_apollo_learner_model.py --subject-slug macroeconomics
        python -m scripts.seed_canon_projection

    This script then drives the live web API for contrasting teaching sessions on
    the served problem. The original Bernoulli use ran one STRONG + one WEAK
    explanation on a single problem; the generalized form drives N VARIATIONS
    (strong / partial / weak) for EACH macro problem, every variation on its own
    cold-start user (so session personalization serves the right problem), clicks
    Done, and inspects Neo4j + Postgres grading evidence. It prints a comparison
    report and writes a JSON dump next to itself.

    Concept/course resolution is parametric (``--concept-slug`` / ``--subject-slug``)
    and, for macro problems, each scenario carries its own Hoot intro transcript
    that steers ``infer_concept_id`` toward the intended concept. The bernoulli
    behavior is preserved: a bare run keyed on ``bernoulli_height_change_find_v2``
    still drives the strong/weak pair on the fluid_mechanics course.

SEPARATION
    This is read/drive-only against shared local infra. It does NOT touch the
    provisioning generation modules and does NOT restart any worker/web process.
    Its users (apollo.probe.*) are dedicated so it cannot collide with other
    work running against the same stack.

USAGE (after dot-sourcing scripts/load_local_env.ps1, or it loads .env/.env.local itself)
    python scripts/apollo_grade_probe.py                       # bernoulli strong+weak (legacy)
    python scripts/apollo_grade_probe.py --mode strong         # one variation only
    python scripts/apollo_grade_probe.py \\                     # the macro sweep
        --subject-slug macroeconomics --concept-slug gdp_components \\
        --variations strong,partial,weak --macro
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

# Repo root importable + load env the same way server.py does (.env then .env.local).
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / ".env.local", override=True)
except Exception:  # noqa: BLE001 - env may already be in the shell
    pass

from neo4j import GraphDatabase  # noqa: E402

from scripts._macro_scenarios import (  # noqa: E402
    MACRO_PROBLEM_IDS,
    MACRO_SCENARIOS,
    macro_transcript,
    macro_variation_messages,
)

WEB_BASE = os.getenv("WEB_BASE_URL", "http://127.0.0.1:8000")
SUPABASE_URL = os.getenv("SUPABASE_URL", "http://127.0.0.1:54321")
ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL", "")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "local_password123")

PROBE_PASSWORD = "Probe-Password-123"

# Legacy bernoulli Hoot intro transcript (used when no macro scenario is served).
TRANSCRIPT = (
    "I'm trying to understand Bernoulli's equation. How do I find the speed of "
    "water draining from a tall open reservoir down a pipe to the ground? It "
    "involves pressure, the height drop, and the velocity of the water."
)

# Strong vs weak teaching for the deterministically-served intro problem
# `bernoulli_height_change_find_v2` (problem_02). Reference graph has 4 nodes:
#   bernoulli (eq) -> equal_pressure_simplification (simp)
#   -> plan_apply_equal_pressure_simplification (proc1)
#   -> plan_set_v1_zero_and_solve_bernoulli (proc2)
_BERNOULLI_SCENARIOS: dict[str, dict[str, list[str]]] = {
    "bernoulli_height_change_find_v2": {
        "strong": [
            "We start from Bernoulli's equation, conservation of energy along a "
            "streamline: P1 + (1/2)*rho*v1**2 + rho*g*h1 = P2 + (1/2)*rho*v2**2 + "
            "rho*g*h2.",
            "Both the reservoir surface at the top and the pipe outlet at the "
            "bottom are open to the atmosphere, so P1 = P2 and the two pressure "
            "terms cancel out of Bernoulli, leaving (1/2)*rho*v1**2 + rho*g*h1 = "
            "(1/2)*rho*v2**2 + rho*g*h2.",
            "Because the reservoir is wide, the surface velocity v1 is about 0. "
            "Substituting v1 = 0, h1 = 20, h2 = 0 and g = 9.81 into the simplified "
            "equation and solving gives v2 = sqrt(2*g*h1) = sqrt(2*9.81*20), which "
            "is about 19.8 m/s.",
        ],
        "weak": [
            "Use Bernoulli's equation, P1 + (1/2)*rho*v1**2 + rho*g*h1 = P2 + "
            "(1/2)*rho*v2**2 + rho*g*h2, and solve it for v2.",
        ],
    },
}

# Every scenario, keyed by problem id: the 5 macro problems (strong/partial/weak)
# plus the legacy bernoulli problem (strong/weak). Macro wins on a key clash
# (there is none — the ids are disjoint).
SCENARIOS: dict[str, dict[str, list[str]]] = {**_BERNOULLI_SCENARIOS, **MACRO_SCENARIOS}

# Fallback if a different problem is served (e.g. personalization changed the pick).
GENERIC = {
    "strong": [
        "Start from the governing equation for this problem and write it out "
        "symbolically with all of its terms.",
        "State the condition or simplification that applies here and explain why "
        "it lets you drop or equate terms.",
        "Lay out the procedure: which equation to apply first, what to substitute, "
        "and how to solve for the target unknown, then compute the number.",
    ],
    "partial": [
        "Start from the governing equation for this problem and write it out "
        "symbolically with all of its terms.",
        "Lay out the procedure: which equation to apply first, what to substitute, "
        "and how to solve for the target unknown, then compute the number.",
    ],
    "weak": [
        "Just use the main equation and solve it for the unknown.",
    ],
}


def scenario_messages(problem_id: str, variation: str) -> list[str]:
    """Authored ``/chat`` messages for a (problem, variation).

    Resolution order: an exact macro/bernoulli scenario for the served problem,
    else the GENERIC fallback for the variation. The GENERIC fallback also
    backfills a variation a scenario doesn't define (e.g. bernoulli has no
    ``partial``), so the N-variation loop never KeyErrors on a served problem.
    """
    scenario = SCENARIOS.get(problem_id)
    if scenario is not None and variation in scenario:
        return scenario[variation]
    return GENERIC[variation]


def intro_transcript(problem_id: str | None) -> str:
    """Hoot intro transcript that steers ``from_hoot`` toward ``problem_id``.

    Macro problems carry a per-problem transcript; anything else falls back to
    the legacy bernoulli transcript (the original single-problem behavior).
    """
    if problem_id is not None:
        macro = macro_transcript(problem_id)
        if macro is not None:
            return macro
    return TRANSCRIPT


# --------------------------------------------------------------------------- #
# Auth + course enrolment (self-contained; dedicated probe users)             #
# --------------------------------------------------------------------------- #
def ensure_user(email: str) -> str:
    """Create-or-find a confirmed auth user via the GoTrue admin API."""
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
         "Content-Type": "application/json"}
    r = requests.post(f"{SUPABASE_URL}/auth/v1/admin/users", headers=h,
                      json={"email": email, "password": PROBE_PASSWORD,
                            "email_confirm": True}, timeout=30)
    if r.status_code in (200, 201):
        return r.json()["id"]
    listing = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=h, timeout=30)
    listing.raise_for_status()
    body = listing.json()
    users = body.get("users", body if isinstance(body, list) else [])
    for u in users:
        if u.get("email") == email:
            return u["id"]
    raise SystemExit(f"could not create/find user {email}: {r.status_code} {r.text}")


def _resolve_space_sql(concept_slug: str | None, subject_slug: str | None) -> tuple[str, dict[str, Any]]:
    """Build the parametric search_space_id lookup SQL + bind params.

    Filters apollo_concepts/apollo_subjects by ``concept_slug`` and/or
    ``subject_slug`` when given; with neither it falls back to the legacy
    bernoulli concept so a bare run is unchanged. Pure — returns SQL text + binds
    so the query is unit-testable without a DB.
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if concept_slug:
        where.append("c.slug = :concept_slug")
        params["concept_slug"] = concept_slug
    if subject_slug:
        where.append("subj.slug = :subject_slug")
        params["subject_slug"] = subject_slug
    if not where:
        where.append("c.slug = :concept_slug")
        params["concept_slug"] = "bernoulli_principle"
    sql = (
        "SELECT subj.search_space_id FROM apollo_concepts c "
        "JOIN apollo_subjects subj ON subj.id = c.subject_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY subj.search_space_id LIMIT 1"
    )
    return sql, params


async def _resolve_course_and_enrol(
    uid: str,
    *,
    concept_slug: str | None = None,
    subject_slug: str | None = None,
) -> int:
    """Resolve the course that owns the target concept/subject and ensure `uid`
    is a member of it. Returns the search_space_id.

    With no slugs this resolves the legacy bernoulli concept (unchanged
    behavior); ``concept_slug`` / ``subject_slug`` narrow it for the macro sweep.
    """
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from database.models import CourseMembership

    sql, params = _resolve_space_sql(concept_slug, subject_slug)
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            space_id = (await s.execute(text(sql), params)).scalar_one_or_none()
            if space_id is None:
                raise SystemExit(
                    f"no concept matching {params!r} — run the seed scripts first"
                )
            existing = (await s.execute(select(CourseMembership).where(
                CourseMembership.user_id == uid,
                CourseMembership.search_space_id == space_id,
            ))).scalar_one_or_none()
            if existing is None:
                s.add(CourseMembership(user_id=uid, search_space_id=space_id, role="student"))
                await s.commit()
            return int(space_id)
    finally:
        await engine.dispose()


def sign_in(email: str) -> str:
    r = requests.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                      headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
                      json={"email": email, "password": PROBE_PASSWORD}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


# --------------------------------------------------------------------------- #
# DB + Neo4j evidence                                                          #
# --------------------------------------------------------------------------- #
async def _fetch_reference(problem_id: str) -> dict[str, Any]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            payload = (await s.execute(text(
                "SELECT payload FROM apollo_concept_problems WHERE problem_code = :pid"
            ), {"pid": problem_id})).scalar_one_or_none()
            return payload or {}
    finally:
        await engine.dispose()


async def _fetch_graphsim_evidence(attempt_id: int) -> dict[str, Any]:
    """Best-effort dump of the graph-simulation grading audit trail."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    out: dict[str, Any] = {}
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            try:
                rows = (await s.execute(text(
                    "SELECT * FROM apollo_graph_comparison_runs WHERE attempt_id = :aid"
                ), {"aid": attempt_id})).mappings().all()
                out["comparison_runs"] = [dict(r) for r in rows]
            except Exception as e:  # noqa: BLE001
                out["comparison_runs_error"] = str(e)
            try:
                n = (await s.execute(text(
                    "SELECT count(*) FROM apollo_mastery_events WHERE attempt_id = :aid"
                ), {"aid": attempt_id})).scalar_one()
                out["mastery_events"] = int(n)
            except Exception as e:  # noqa: BLE001
                out["mastery_events_error"] = str(e)
        return out
    finally:
        await engine.dispose()


def _neo4j_stats(driver, attempt_id: int) -> dict[str, Any]:
    with driver.session() as s:
        by_source = {r["source"]: r["c"] for r in s.run(
            "MATCH (n:_KGNode {attempt_id:$aid}) RETURN n.source AS source, count(*) AS c",
            aid=attempt_id)}
        edges = {r["t"]: r["c"] for r in s.run(
            "MATCH (:_KGNode {attempt_id:$aid})-[e]->(:_KGNode {attempt_id:$aid}) "
            "RETURN type(e) AS t, count(*) AS c", aid=attempt_id)}
        stamp = s.run(
            "MATCH (n:_KGNode {attempt_id:$aid}) "
            "RETURN count(n) AS total, count(n.graded_at) AS stamped", aid=attempt_id).single()
        node_labels = [dict(r) for r in s.run(
            "MATCH (n:_KGNode {attempt_id:$aid}) "
            "RETURN [l IN labels(n) WHERE l <> '_KGNode'][0] AS kind, n.node_id AS node_id, "
            "n.source AS source ORDER BY kind, node_id", aid=attempt_id)]
    return {
        "nodes_total": stamp["total"],
        "nodes_by_source": by_source,
        "edges_by_type": edges,
        "graded_at_stamped": stamp["stamped"],
        "nodes": node_labels,
    }


def _canon_count(driver) -> int:
    with driver.session() as s:
        return s.run("MATCH (c:Canon) RETURN count(c) AS c").single()["c"]


# --------------------------------------------------------------------------- #
# Session driving                                                             #
# --------------------------------------------------------------------------- #
def _post(token: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    r = requests.post(f"{WEB_BASE}{path}",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json=body or {}, timeout=120)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, r.text


def run_mode(
    driver,
    token: str,
    search_space_id: int,
    mode: str,
    *,
    difficulty: str = "intro",
    transcript: str | None = None,
) -> dict[str, Any]:
    """Drive one teaching session for ``mode`` (a strong/partial/weak variation).

    ``transcript`` overrides the Hoot intro used to seed the session (so a macro
    sweep can steer ``from_hoot`` to a specific problem); it defaults to the
    legacy bernoulli transcript. ``difficulty`` lets the macro sweep request a
    standard-difficulty problem. The result carries ``variation`` (alias of
    ``mode``) so the score matrix can key on it."""
    print(f"\n{'='*70}\n[{mode.upper()}] starting session\n{'='*70}")
    code, start = _post(token, "/apollo/sessions/from_hoot",
                        {"hoot_transcript": transcript or TRANSCRIPT,
                         "difficulty": difficulty,
                         "search_space_id": search_space_id})
    if code != 200:
        return {"mode": mode, "variation": mode, "error": f"from_hoot {code}: {start}"}
    session_id = start["session_id"]
    attempt_id = start["attempt_id"]
    problem = start["problem"]
    pid = problem["id"]
    print(f"  served problem: {pid} (difficulty={problem['difficulty']}, "
          f"target={problem['target_unknown']})")

    msgs = scenario_messages(pid, mode)
    reference = asyncio.run(_fetch_reference(pid))
    ref_steps = reference.get("reference_solution", [])
    ref_by_type: dict[str, int] = {}
    for st in ref_steps:
        ref_by_type[st["entry_type"]] = ref_by_type.get(st["entry_type"], 0) + 1
    print(f"  reference graph: {len(ref_steps)} nodes {ref_by_type}")

    turns = []
    for i, m in enumerate(msgs, 1):
        ccode, cresp = _post(token, f"/apollo/sessions/{session_id}/chat", {"message": m})
        added = cresp.get("kg_entries_added") if isinstance(cresp, dict) else None
        print(f"  turn {i}: HTTP {ccode}  kg_added={added}")
        turns.append({"message": m, "status": ccode,
                      "kg_entries_added": added,
                      "reply": cresp.get("message") if isinstance(cresp, dict) else cresp})

    dcode, done = _post(token, f"/apollo/sessions/{session_id}/done")
    if dcode != 200:
        return {"mode": mode, "variation": mode, "session_id": session_id,
                "attempt_id": attempt_id, "served_problem": pid, "turns": turns,
                "error": f"done {dcode}: {done}"}

    neo = _neo4j_stats(driver, attempt_id)
    evidence = asyncio.run(_fetch_graphsim_evidence(attempt_id))
    _post(token, f"/apollo/sessions/{session_id}/end")

    rubric = done.get("rubric", {})
    coverage = done.get("coverage", {})
    per_step = coverage.get("per_step", {})
    covered = [k for k, v in per_step.items() if v == "covered"]
    missing = [k for k, v in per_step.items() if v != "covered"]
    overall = rubric.get("overall", {})
    print(f"  DONE  overall={overall.get('score')} ({overall.get('letter')})  "
          f"covered={len(covered)}/{len(per_step)}  missing={missing}")
    print(f"  Neo4j: {neo['nodes_total']} student nodes {neo['nodes_by_source']}, "
          f"edges {neo['edges_by_type']}, graded_at stamped={neo['graded_at_stamped']}")
    print(f"  graph-sim: comparison_runs={len(evidence.get('comparison_runs', []))} "
          f"mastery_events={evidence.get('mastery_events')}")

    return {
        "mode": mode, "variation": mode, "session_id": session_id,
        "attempt_id": attempt_id,
        "served_problem": pid, "reference_node_count": len(ref_steps),
        "reference_by_type": ref_by_type,
        "turns": turns,
        "rubric": rubric,
        "coverage_per_step": per_step,
        "covered": covered, "missing": missing,
        "procedure_scores": coverage.get("procedure_scores", {}),
        "diagnostic_narrative": done.get("diagnostic_narrative"),
        "solver_indicator": done.get("solver_indicator"),
        "xp_earned": done.get("xp_earned"),
        "neo4j": neo,
        "graphsim_evidence": evidence,
    }


def parse_variations(raw: str | None, *, mode: str = "both") -> list[str]:
    """Resolve the ordered list of variations to run.

    ``--variations`` (comma-separated, e.g. ``strong,partial,weak``) wins when
    given. Otherwise ``--mode`` maps the legacy choices: ``both`` -> strong+weak;
    a single mode -> just that one. Unknown names are rejected (fail fast).
    """
    allowed = {"strong", "partial", "weak"}
    if raw:
        wanted = [v.strip() for v in raw.split(",") if v.strip()]
        bad = [v for v in wanted if v not in allowed]
        if bad:
            raise SystemExit(f"unknown variation(s) {bad}; allowed: {sorted(allowed)}")
        return wanted
    return ["strong", "weak"] if mode == "both" else [mode]


def build_sweep(
    *,
    variations: list[str],
    macro: bool,
    problem: str | None,
) -> list[tuple[str | None, str]]:
    """Plan the (problem_id, variation) attempts to run, in order.

    * macro sweep (``macro`` true, no ``problem``): every macro problem × every
      variation (15 attempts for 5 problems × strong/partial/weak).
    * macro sweep pinned to one ``problem``: that problem × every variation.
    * legacy (``macro`` false, no ``problem``): one ``(None, variation)`` per
      variation — ``None`` means "let the served problem decide the transcript"
      (the bernoulli single-problem behavior).
    """
    if problem is not None:
        return [(problem, v) for v in variations]
    if macro:
        return [(pid, v) for pid in MACRO_PROBLEM_IDS for v in variations]
    return [(None, v) for v in variations]


# Distinct (concept, difficulty) per problem so from_hoot serves each macro
# problem UNIQUELY: session_init picks the FIRST problem at the requested
# difficulty, so two problems sharing a concept+difficulty would collide (only
# the first would ever be served). The difficulty here is a deterministic
# routing key for the sweep, not a pedagogical claim — see the experiment
# DESIGN.md. Within gdp_components: intro/standard/hard; within
# nominal_vs_real_gdp: standard/hard.
_DIFFICULTY_BY_PROBLEM: dict[str, str] = {
    "gdp_identity": "intro",
    "net_exports_sign": "standard",
    "nnp_chain": "hard",
    "real_gdp_from_deflator": "standard",
    "real_gdp_growth": "hard",
}


def _difficulty_for(problem_id: str | None) -> str:
    """The from_hoot difficulty that surfaces a given macro problem uniquely.

    Each macro problem has a distinct difficulty WITHIN its concept, so
    session_init's "first problem at the requested difficulty" serves exactly
    one. Unknown ids (and the legacy bernoulli path) default to ``intro``.
    """
    return _DIFFICULTY_BY_PROBLEM.get(problem_id or "", "intro")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["strong", "partial", "weak", "both"], default="both")
    ap.add_argument("--variations", default=None,
                    help="comma-separated variations to run (e.g. strong,partial,weak); "
                         "overrides --mode when set")
    ap.add_argument("--macro", action="store_true",
                    help="run the full macro sweep (every macro problem x variation)")
    ap.add_argument("--problem", default=None,
                    help="pin the sweep to one problem id (drives its Hoot transcript)")
    ap.add_argument("--concept-slug", default=None,
                    help="resolve the course via this concept slug (else the subject / legacy)")
    ap.add_argument("--subject-slug", default=None,
                    help="resolve the course via this subject slug (e.g. macroeconomics)")
    ap.add_argument("--tag", default="",
                    help="suffix for probe user emails — use a fresh tag to force "
                         "first-attempt cold-start users (removes reattempt confound)")
    args = ap.parse_args(argv)
    for name, val in (("SUPABASE_ANON_KEY", ANON_KEY), ("SUPABASE_SERVICE_ROLE_KEY", SERVICE_KEY),
                      ("SUPABASE_DB_URL/DATABASE_URL", DB_URL)):
        if not val:
            raise SystemExit(f"missing env {name} — dot-source scripts/load_local_env.ps1 first")

    variations = parse_variations(args.variations, mode=args.mode)
    sweep = build_sweep(variations=variations, macro=args.macro, problem=args.problem)

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    canon = _canon_count(driver)
    print(f":Canon nodes in Neo4j: {canon}")

    results = []
    for problem_id, variation in sweep:
        slug = f"{problem_id}." if problem_id else ""
        email = f"apollo.probe.{slug}{variation}{args.tag}@example.com"
        uid = ensure_user(email)
        space_id = asyncio.run(_resolve_course_and_enrol(
            uid, concept_slug=args.concept_slug, subject_slug=args.subject_slug
        ))
        token = sign_in(email)
        print(f"\nuser {email} ({uid}) enrolled in course {space_id} "
              f"[problem={problem_id} variation={variation}]")
        result = run_mode(
            driver, token, space_id, variation,
            difficulty=_difficulty_for(problem_id),
            transcript=intro_transcript(problem_id),
        )
        results.append(result)
    driver.close()

    report = {"canon_nodes": canon, "web_base": WEB_BASE, "results": results}
    out_path = Path(__file__).resolve().parent / "apollo_grade_probe_report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"\n{'#'*70}\nSUMMARY\n{'#'*70}")
    for r in results:
        variation = r.get("variation", r.get("mode"))
        served = r.get("served_problem", "?")
        if "error" in r:
            print(f"  {served}/{variation}: ERROR {r['error']}")
            continue
        ov = r["rubric"].get("overall", {})
        print(f"  {served}/{variation:7s}: overall={ov.get('score')} ({ov.get('letter')})  "
              f"covered={len(r['covered'])}/{r['reference_node_count']}  "
              f"missing={r['missing']}  neo4j_nodes={r['neo4j']['nodes_total']}  "
              f"graphsim_runs={len(r['graphsim_evidence'].get('comparison_runs', []))}")

    graded = [r for r in results if "rubric" in r]
    by_var = {r.get("variation", r.get("mode")): r for r in graded}
    if "strong" in by_var and "weak" in by_var:
        s_ov = by_var["strong"]["rubric"].get("overall", {}).get("score", 0) or 0
        w_ov = by_var["weak"]["rubric"].get("overall", {}).get("score", 0) or 0
        verdict = "DISCRIMINATES (strong > weak)" if s_ov > w_ov else "DID NOT discriminate"
        print(f"\n  VERDICT: {verdict}  (strong={s_ov} vs weak={w_ov})")
    print(f"\nfull report -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
