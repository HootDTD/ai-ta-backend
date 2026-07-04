"""F1b ad-hoc driver: run the FULL live persona corpus (fluid_mechanics,
macroeconomics, linear_motion) through one real Apollo session per persona,
one subject "chunk" at a time.

Not production code -- scratch glue for the one-off F1b task per the brief
("driver script under campaign/out/f1/, not campaign/cast/ or
campaign/orchestrate.py which does not exist on this branch yet" -- see
campaign/out/f1/run_s1_s2.py precedent from F1a).

Wires the REAL seams from campaign/cast/student.py:
  - HttpxApolloClient  (real HTTP against the locally-running uvicorn)
  - default_chat_fn    (real OpenAI call playing the persona)
  - SqlArtifactReader  (real DB read of the two GradingArtifact rows)
  - mint_student_token (real Supabase local-auth admin-API mint) -- ONE
    fresh student identity per persona (not one shared student), so
    concurrent/park-per-persona session state never collides.

linear_motion's persona files are PROVISIONAL (their expected-ledgers are
authored against a hand-written reference graph, not the real minted
apollo_kg_entities keys -- see campaign/README.md "Known gaps" /
campaign/cast/personas/validate.py PROVISIONAL_SUBJECTS). AttemptRecord
(campaign/cast/student.py) has no provisional field, so this driver stamps
a "provisional": true key directly onto the JSONL line for that subject's
attempts instead of silently treating them as equally-trustworthy ground
truth -- flagged here per the task brief.

Usage (anaconda/base interpreter has no torch dependency need here -- this
script only talks HTTP/DB/OpenAI, never imports apollo.resolution.*):
    python -m campaign.out.f1.run_f1b_corpus --subject fluid_mechanics
    python -m campaign.out.f1.run_f1b_corpus --subject macroeconomics
    python -m campaign.out.f1.run_f1b_corpus --subject linear_motion
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env(REPO_ROOT / ".env.campaign")

# Apollo flags for this campaign run (mirrors campaign/out/f1/stack-state.md
# "Exact command" block -- the already-running server process has these
# baked in; re-asserted here only so any script-side flag reads agree).
os.environ.setdefault("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "1")
os.environ.setdefault("APOLLO_CLARIFICATION_ENABLED", "1")
os.environ.setdefault("APOLLO_NLI_ENABLED", "1")
os.environ.setdefault("APOLLO_GRAPH_GRADER_LIVE", "0")
os.environ.setdefault("APOLLO_GRADING_ARTIFACT_ENABLED", "1")

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from campaign.cast.personas.schema import PersonaAttempt  # noqa: E402
from campaign.cast.personas.validate import (  # noqa: E402
    PERSONAS_DIR,
    PROVISIONAL_SUBJECTS,
    iter_persona_files,
    load_persona_file,
)
from campaign.cast.student import (  # noqa: E402
    HttpxApolloClient,
    SqlArtifactReader,
    default_chat_fn,
    mint_student_token,
    run_attempt,
)

BASE_URL = "http://127.0.0.1:8010"  # F2 isolated backend
SEARCH_SPACE_ID = 1  # aita_search_spaces slug=campaign-course, see stack-state.md
STUDENT_PASSWORD = "CampaignStudentF2123!"
OUT_DIR = Path(__file__).resolve().parent
ATTEMPTS_PATH = OUT_DIR / "attempts.jsonl"
_SUBJECTS_ROOT = REPO_ROOT / "apollo" / "subjects"


#: Discovered live (smoke test, first attempt): the driver's difficulty=
#: "standard" default almost never matches a persona's authored problem_id --
#: each concept has SEVERAL problems at DIFFERENT `difficulty` values
#: (fluid_mechanics bernoulli_principle: 4 problems at "intro" + 1 at
#: "standard"; macroeconomics: mixed intro/standard/hard). Session creation
#: and every `/next` re-roll both take a `difficulty` argument (see
#: campaign/cast/student.py run_attempt), so this resolves the REAL
#: difficulty the persona's problem actually lives at instead of hardcoding
#: one value -- otherwise `_resolve_problem`'s retries would either always
#: land on the single "standard" problem (fluid_mechanics) or exhaust the
#: pool (PoolExhaustedError -> 409, an unhandled exception in
#: campaign/cast/student.py`_resolve_problem`, which was observed turning an
#: otherwise-good attempt into status="error").
def _difficulty_for(persona: PersonaAttempt) -> str:
    concept_dir = _SUBJECTS_ROOT / persona.subject / "concepts" / persona.concept / "problems"
    if concept_dir.is_dir():
        for path in sorted(concept_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("id") == persona.problem_id:
                return str(data.get("difficulty", "standard"))
    # PROVISIONAL subjects (linear_motion): the real minted apollo_concept_problems
    # rows use scrape-hash ids, not the persona's authored problem_id, so no
    # difficulty match is possible here by construction (see
    # campaign/README.md "Known gaps" / PROVISIONAL_SUBJECTS) -- problem_matched
    # will legitimately read False for these regardless of difficulty chosen.
    return "intro"


def _personas_for_subject(subject: str) -> list[tuple[Path, PersonaAttempt]]:
    files = [p for p in iter_persona_files(PERSONAS_DIR) if p.parent.name == subject]
    files.sort()
    return [(p, load_persona_file(p)) for p in files]


def _student_email(subject: str, path: Path) -> str:
    # One dedicated student per persona file -- stable across re-runs of the
    # same chunk (mint_student_token is idempotent: admin/users create is a
    # no-op 4xx if the email already exists, then sign-in proceeds).
    return f"f2-{subject}-{path.stem}@campaign.local".lower()


def _session_factory():
    dsn = os.environ["SUPABASE_DB_URL"]
    engine = create_async_engine(dsn, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _append_jsonl(record, path: Path, *, extra: dict) -> None:
    payload = record.to_jsonl_dict()
    payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True))
        fh.write("\n")


async def run_subject(subject: str) -> None:
    pairs = _personas_for_subject(subject)
    if not pairs:
        print(f"[{subject}] NO PERSONA FILES FOUND -- aborting chunk", flush=True)
        return

    provisional = subject in PROVISIONAL_SUBJECTS
    client = HttpxApolloClient(base_url=BASE_URL)
    artifact_reader = SqlArtifactReader(_session_factory())

    supabase_url = os.environ["SUPABASE_URL"]
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    log_path = OUT_DIR / f"chunk-{subject}.log"
    n_ok = 0
    n_err = 0
    consecutive_errors_at_start = 0
    aborted = False

    with log_path.open("w", encoding="utf-8") as log:

        def logline(msg: str) -> None:
            stamped = f"[{time.strftime('%H:%M:%S')}] {msg}"
            print(stamped, flush=True)
            log.write(stamped + "\n")
            log.flush()

        logline(f"=== chunk {subject}: {len(pairs)} personas (provisional={provisional}) ===")

        for idx, (path, persona) in enumerate(pairs):
            email = _student_email(subject, path)
            t0 = time.monotonic()
            try:
                token = await mint_student_token(
                    email=email,
                    password=STUDENT_PASSWORD,
                    supabase_url=supabase_url,
                    service_role_key=service_role_key,
                )
            except Exception as exc:  # noqa: BLE001
                n_err += 1
                logline(f"MINT-TOKEN FAILED {path.name}: {exc!r}")
                if idx < 2:
                    consecutive_errors_at_start += 1
                if consecutive_errors_at_start >= 2:
                    logline(
                        f"ABORTING chunk {subject}: first {consecutive_errors_at_start} "
                        "attempts failed at token-mint -- looks systemic (auth/route), "
                        "not a per-attempt issue. Stopping to preserve the rest of the corpus."
                    )
                    aborted = True
                    break
                continue

            difficulty = _difficulty_for(persona)
            record = await run_attempt(
                persona,
                client=client,
                chat_fn=default_chat_fn,
                artifact_reader=artifact_reader,
                token=token,
                search_space_id=SEARCH_SPACE_ID,
                difficulty=difficulty,
            )
            dt = time.monotonic() - t0
            # O1 latency sidecar: total attempt wall-clock (session + chat +
            # Done + artifact readback). The grading-chain latency itself is
            # grading_latency_ms inside the canonical artifact payload.
            extra: dict = {"wall_seconds": round(dt, 2)}
            if provisional:
                extra["provisional"] = True
            _append_jsonl(record, ATTEMPTS_PATH, extra=extra)
            if record.status == "ok":
                n_ok += 1
                logline(
                    f"OK   {path.name} attempt_id={record.attempt_id} "
                    f"matched={record.problem_matched} scorecard={'yes' if record.scorecard else 'no'} "
                    f"canonical={'yes' if record.artifact_canonical else 'no'} "
                    f"pair={'yes' if record.artifact_pair else 'no'} ({dt:.1f}s)"
                )
            else:
                n_err += 1
                logline(f"ERR  {path.name}: {record.error} ({dt:.1f}s)")
                if idx < 2:
                    consecutive_errors_at_start += 1
                if consecutive_errors_at_start >= 2:
                    logline(
                        f"ABORTING chunk {subject}: first {consecutive_errors_at_start} "
                        "attempts errored -- looks systemic, not per-attempt. Stopping."
                    )
                    aborted = True
                    break

        logline(
            f"=== chunk {subject} done: ok={n_ok} err={n_err} total_seen={n_ok + n_err} "
            f"aborted={aborted} ==="
        )

    print(
        f"CHUNK-SUMMARY subject={subject} ok={n_ok} err={n_err} aborted={aborted}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    args = parser.parse_args()
    asyncio.run(run_subject(args.subject))


if __name__ == "__main__":
    main()
