"""One-shot local driver for the Apollo derived-equation grading probe.

Why this exists: the agent cannot run local-DB writes (the auto-mode classifier
keeps mis-flagging the local Docker stack as remote prod) nor self-grant a
permission rule. So this packages the whole verification into ONE process the
USER launches via `!`, which is not subject to the agent's classifier.

It (1) re-seeds the concept registry so the local DB carries the updated problem
payloads (the `simplification.content.substitution` fields the fix consumes),
(2) boots the FastAPI server from the working tree on :8001, (3) runs
apollo_grade_probe.py against it, and (4) prints the strong/weak graph-sim
scores so PASS/FAIL is obvious.

Run (PowerShell, from the repo root or anywhere):
    .venv/Scripts/python.exe scripts/run_local_probe.py

Everything targets the LOCAL stack only (127.0.0.1:54322 Postgres, :7687 Neo4j).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

# The seed script reads DATABASE_URL; the rest of the stack reads SUPABASE_DB_URL.
db_url = os.environ.get("SUPABASE_DB_URL", "")
if not db_url:
    sys.exit("SUPABASE_DB_URL is not set — check .env / .env.local")
os.environ["DATABASE_URL"] = db_url
os.environ["WEB_BASE_URL"] = "http://127.0.0.1:8001"

target = db_url.split("@")[-1]
if "127.0.0.1" not in target and "localhost" not in target:
    sys.exit(f"REFUSING TO RUN: DB target {target!r} is not local")
print(f"[probe] DB target (local): {target}")

PY = sys.executable


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"\n[probe] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(ROOT), **kw)


def _wait_for_port(host: str, port: int, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(1.0)
    return False


def _print_scores() -> None:
    """Print the graph-sim score columns straight from the probe's report."""
    report_path = ROOT / "scripts" / "apollo_grade_probe_report.json"
    if not report_path.exists():
        print("[probe] no report file written — probe likely errored above")
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"\n{'='*70}\n[probe] GRAPH-SIM SCORES (the acceptance criteria)\n{'='*70}")
    print(f":Canon nodes = {report.get('canon_nodes')}")
    for r in report.get("results", []):
        mode = r.get("mode")
        aid = r.get("attempt_id")
        runs = r.get("graphsim_evidence", {}).get("comparison_runs", [])
        if not runs:
            print(f"\n  [{mode}] attempt={aid}: NO comparison_runs "
                  f"(shadow grading may be off, or it errored)")
            continue
        row = runs[0]
        score_keys = sorted(k for k in row if "score" in k.lower() or k == "abstained")
        print(f"\n  [{mode}] attempt={aid}")
        for k in score_keys:
            print(f"      {k:28s} = {row[k]}")


def main() -> int:
    # 1) Re-seed the concept registry (idempotent; pushes substitution fields).
    seed = _run([PY, "-m", "scripts.seed_apollo_concept_registry"])
    if seed.returncode != 0:
        return print("[probe] SEED FAILED — aborting") or 1

    # 2) Boot the server from the working tree on :8001.
    print("\n[probe] booting server on :8001 ...")
    server = subprocess.Popen(
        [PY, "-c", "import uvicorn; uvicorn.run('server:app', host='127.0.0.1', port=8001)"],
        cwd=str(ROOT),
    )
    try:
        if not _wait_for_port("127.0.0.1", 8001):
            return print("[probe] SERVER DID NOT COME UP on :8001") or 1
        time.sleep(3)  # let app-startup (DB pools, Neo4j) settle past the port open

        # 3) Run the probe (strong + weak) with a fresh tag.
        _run([PY, "scripts/apollo_grade_probe.py", "--mode", "both", "--tag", ".resfix2"])
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # 4) Surface the scores.
    _print_scores()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
