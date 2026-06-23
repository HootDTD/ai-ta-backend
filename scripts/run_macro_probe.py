"""One-shot local orchestrator for the Macro Ch.6 graph-grading probe.

Modeled on ``scripts/run_local_probe.py`` but for the macro experiment
(``docs/experiments/2026-06-22-macro-graph-grading-probe/DESIGN.md``). It packages
the whole local run into ONE process the USER launches (the agent's auto-mode
classifier keeps mis-flagging the local Docker stack as remote, so the agent
cannot do the DB writes itself):

  1. verify/embed   — embed the Ch.6 PDF into local pgvector via
                      ``scripts/index_local_pdf.py`` IF the macro corpus is not
                      already embedded for the macro course.
  2. seed x3        — ``seed_apollo_concept_registry`` ->
                      ``seed_apollo_learner_model --subject-slug macroeconomics`` ->
                      ``seed_canon_projection`` (ORDER MATTERS).
  3. mine + faithfulness (optional, behind ``--skip-mining``) — scrape §6.1–6.2
                      chunks for candidate questions and faithfulness-check the
                      authored reference solutions against retrieved spans.
  4. boot :8001     — ``uvicorn server:app`` from the working tree.
  5. probe          — ``scripts/apollo_grade_probe.py`` over the macro concepts
                      across all 3 variations (strong/partial/weak).
  6. score matrix   — read ``apollo_graph_comparison_runs`` for the produced
                      attempts and print + persist a per-(problem, variation)
                      score matrix to the probe report JSON.

Everything targets the LOCAL stack only (127.0.0.1 Postgres + Neo4j); a non-local
``SUPABASE_DB_URL`` aborts before any subprocess, server boot, or seed runs.

Run (PowerShell, from ai-ta-backend/, after dot-sourcing the local env)::

    . .\\scripts\\load_local_env.ps1
    .venv\\Scripts\\python.exe scripts\\run_macro_probe.py --tag .macro1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess  # noqa: S404 - local-only subprocess orchestration (guarded).
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts._macro_probe_report import (  # noqa: E402
    LocalTargetError,
    build_score_matrix,
    db_target,
    format_score_matrix,
    require_local_target,
)

log = logging.getLogger("run_macro_probe")

SUBJECT_SLUG = "macroeconomics"
PROBE_HOST = "127.0.0.1"
PROBE_PORT = 8001
PROBE_BASE_URL = f"http://{PROBE_HOST}:{PROBE_PORT}"
REPORT_PATH = ROOT / "scripts" / "apollo_grade_probe_report.json"
MATRIX_PATH = ROOT / "scripts" / "macro_probe_score_matrix.json"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no subprocess / HTTP / DB)                        #
# --------------------------------------------------------------------------- #
def prepare_env(environ: dict[str, str]) -> dict[str, str]:
    """Return a NEW env mapping with the probe's required overrides applied.

    The seeders read ``DATABASE_URL`` while the rest of the stack reads
    ``SUPABASE_DB_URL`` — force them equal. Also pin ``WEB_BASE_URL`` so the probe
    drives the :8001 server this orchestrator boots (not a stray :8000). Pure:
    never mutates ``environ`` in place. Raises ``LocalTargetError`` when
    ``SUPABASE_DB_URL`` is missing or non-local.
    """
    db_url = require_local_target(environ.get("SUPABASE_DB_URL", ""))
    merged = dict(environ)
    merged["SUPABASE_DB_URL"] = db_url
    merged["DATABASE_URL"] = db_url
    merged["WEB_BASE_URL"] = PROBE_BASE_URL
    return merged


def seed_commands(py: str, db_url: str, search_space_id: int) -> list[list[str]]:
    """The three seed subprocess argv lists, in dependency order.

    registry (problem rows) -> learner-model (Layer-1 entities + entity_key +
    declared_paths for macroeconomics) -> canon projection (Neo4j :Canon).

    The learner-model + canon steps MUST be scoped to the macro course via
    ``--search-space-id``: the macro subject is pinned to its OWN course, not
    the ``MIN(id)`` fluids course the seeders default to, so an unscoped run
    resolves the wrong (or no) subject and fails.
    """
    sid = str(search_space_id)
    return [
        [py, "-m", "scripts.seed_apollo_concept_registry", "--database-url", db_url],
        [py, "scripts/seed_apollo_learner_model.py",
         "--subject-slug", SUBJECT_SLUG, "--search-space-id", sid, "--database-url", db_url],
        [py, "scripts/seed_canon_projection.py",
         "--search-space-id", sid, "--database-url", db_url],
    ]


def index_command(py: str, pdf: str, search_space_id: int) -> list[str]:
    """The ``index_local_pdf.py`` argv to embed the Ch.6 PDF as a textbook."""
    return [
        py, "scripts/index_local_pdf.py",
        "--pdf", pdf,
        "--search-space-id", str(search_space_id),
        "--material-kind", "textbook",
        "--week", "none",
    ]


def probe_command(py: str, tag: str) -> list[str]:
    """The ``apollo_grade_probe.py`` argv for the full macro sweep (15 attempts)."""
    cmd = [
        py, "scripts/apollo_grade_probe.py",
        "--macro",
        "--subject-slug", SUBJECT_SLUG,
        "--variations", "strong,partial,weak",
    ]
    if tag:
        cmd += ["--tag", tag]
    return cmd


def server_command(py: str) -> list[str]:
    """The uvicorn boot argv for ``server:app`` on the probe host/port."""
    return [
        py, "-c",
        f"import uvicorn; uvicorn.run('server:app', host='{PROBE_HOST}', port={PROBE_PORT})",
    ]


def load_report(report_path: Path) -> dict[str, Any]:
    """Load the probe report JSON, or ``{}`` when it was never written."""
    if not report_path.exists():
        return {}
    return json.loads(report_path.read_text(encoding="utf-8"))


def score_matrix_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the per-(problem, variation) score matrix from a probe report."""
    return build_score_matrix(report.get("results", []))


# --------------------------------------------------------------------------- #
# Impure orchestration (subprocess / HTTP / server / DB)                       #
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=str(ROOT), env=env)  # noqa: S603


def _wait_for_port(host: str, port: int, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(1.0)
    return False


async def _macro_space_id(db_url: str) -> int:
    """Resolve the macro course's search_space_id (subject -> search_space_id)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            space_id = (await conn.execute(text(
                "SELECT search_space_id FROM apollo_subjects WHERE slug = :slug"
            ), {"slug": SUBJECT_SLUG})).scalar_one_or_none()
        if space_id is None:
            raise RuntimeError(
                f"no '{SUBJECT_SLUG}' subject — run _macro_setup_course.py + the "
                "registry seed first"
            )
        return int(space_id)
    finally:
        await engine.dispose()


async def _corpus_embedded(db_url: str, search_space_id: int) -> bool:
    """True iff a ready textbook doc with chunks already exists for the course."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            count = (await conn.execute(text(
                "SELECT count(*) FROM aita_documents d "
                "WHERE d.search_space_id = :sid "
                "AND d.material_kind = 'textbook' "
                "AND d.status->>'state' = 'ready' "
                "AND EXISTS (SELECT 1 FROM aita_chunks c WHERE c.document_id = d.id)"
            ), {"sid": search_space_id})).scalar_one()
        return int(count or 0) > 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[macro-probe] %(message)s")
    parser = argparse.ArgumentParser(description="Local macro Ch.6 graph-grading probe orchestrator.")
    parser.add_argument("--pdf", default=os.getenv("MACRO_CH6_PDF", ""),
                        help="Ch.6 PDF to embed (or set MACRO_CH6_PDF). Only used "
                             "when the corpus is not already embedded.")
    parser.add_argument("--tag", default=".macro1",
                        help="probe user-email suffix (fresh tag => cold-start users)")
    parser.add_argument("--skip-mining", action="store_true",
                        help="skip the §6.1–6.2 question-mining + faithfulness hooks")
    parser.add_argument("--skip-embed", action="store_true",
                        help="never embed even if the corpus is missing (assume present)")
    args = parser.parse_args(argv)

    # 0) env — force DATABASE_URL = SUPABASE_DB_URL, pin WEB_BASE_URL, refuse remote.
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.local", override=True)
    try:
        env = prepare_env(dict(os.environ))
    except LocalTargetError as exc:
        log.error("%s", exc)
        return 2
    db_url = env["SUPABASE_DB_URL"]
    log.info("local DB target: %s", db_target(db_url))

    import asyncio

    space_id = asyncio.run(_macro_space_id(db_url))
    log.info("macro course search_space_id = %s", space_id)

    # 1) verify/embed
    if not args.skip_embed:
        if asyncio.run(_corpus_embedded(db_url, space_id)):
            log.info("corpus already embedded for course %s — skipping embed", space_id)
        elif not args.pdf:
            log.error("corpus not embedded and no --pdf / MACRO_CH6_PDF given — aborting")
            return 1
        else:
            embedded = _run(index_command(sys.executable, args.pdf, space_id), env)
            if embedded.returncode != 0:
                log.error("embed FAILED — aborting")
                return 1

    # 2) seed x3 (order matters; learner-model + canon scoped to the macro course)
    for cmd in seed_commands(sys.executable, db_url, space_id):
        seeded = _run(cmd, env)
        if seeded.returncode != 0:
            log.error("seed step FAILED (%s) — aborting", cmd[2] if len(cmd) > 2 else cmd)
            return 1

    # 3) optional mining + faithfulness hooks (infra-heavy; off with --skip-mining)
    if args.skip_mining:
        log.info("skipping mining + faithfulness hooks (--skip-mining)")
    else:
        rc = _run_mining(db_url, space_id, env)
        if rc != 0:
            log.warning("mining/faithfulness reported issues (rc=%s) — continuing to probe", rc)

    # 4) boot server on :8001
    log.info("booting server on :%s ...", PROBE_PORT)
    server = subprocess.Popen(server_command(sys.executable), cwd=str(ROOT), env=env)  # noqa: S603
    try:
        if not _wait_for_port(PROBE_HOST, PROBE_PORT):
            log.error("server did not come up on :%s", PROBE_PORT)
            return 1
        time.sleep(3)  # let app-startup (DB pools, Neo4j) settle past the port open

        # 5) run the macro probe (15 attempts: 5 problems x strong/partial/weak)
        probed = _run(probe_command(sys.executable, args.tag), env)
        if probed.returncode != 0:
            log.warning("probe exited non-zero (%s) — still reading any scores", probed.returncode)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # 6) score matrix
    report = load_report(REPORT_PATH)
    matrix = score_matrix_from_report(report)
    log.info("\n%s", format_score_matrix(matrix))
    MATRIX_PATH.write_text(
        json.dumps({"canon_nodes": report.get("canon_nodes"),
                    "web_base": report.get("web_base"),
                    "score_matrix": matrix}, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("score matrix -> %s", MATRIX_PATH)
    return 0


def _run_mining(db_url: str, search_space_id: int, env: dict[str, str]) -> int:
    """Run the §6.1–6.2 question-mining + faithfulness hooks (best-effort).

    Delegated to a child process so this orchestrator stays resilient to mining
    failures. The child is the standalone mining driver when present; absent it,
    this is a no-op (returns 0) so the probe still runs.
    """
    mining_script = ROOT / "scripts" / "_macro_mine.py"
    if not mining_script.is_file():
        log.info("no _macro_mine.py present — skipping mining (probe still runs)")
        return 0
    result = _run(
        [sys.executable, str(mining_script), "--search-space-id", str(search_space_id)],
        env,
    )
    return result.returncode


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
