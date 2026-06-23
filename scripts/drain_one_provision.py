"""LOCAL-ONLY one-shot provisioning drain (NOT committed).

Reuses the frozen worker wiring (`apollo.provision_worker._drain_one`) to claim
the FIFO-earliest open job, run `run_provisioning` ONCE with a real MeteredChat
and the default retrieve_fn (= the new course-scoped grounding adapter), and
print the outcome. No poll loop, no flag gate — a controlled single shot for the
manual acceptance run. Env MUST be loaded via scripts/load_local_env.ps1 first so
it targets the LOCAL Supabase + LOCAL Neo4j.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Make the repo root (parent of scripts/) importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _main() -> int:
    # Hard local-only guard (defence in depth on top of the PowerShell guard).
    db_url = os.environ.get("SUPABASE_DB_URL", "")
    neo_uri = os.environ.get("NEO4J_URI", "")
    if "127.0.0.1:54322" not in db_url:
        print(f"ABORT: SUPABASE_DB_URL is not the local DB: {db_url!r}")
        return 2
    if "127.0.0.1:7687" not in neo_uri:
        print(f"ABORT: NEO4J_URI is not local: {neo_uri!r}")
        return 2

    from apollo.persistence.neo4j_client import Neo4jClient
    from apollo.provision_worker import _default_metered_factory, _drain_one
    from database.session import get_async_session

    # --- Phase-1 instrumentation (LOCAL DEBUG ONLY) ------------------------- #
    # The orchestrator records TagMintError with context={} (message discarded).
    # Wrap the stage-4 entrypoint to surface the EXACT failure + the actual
    # minted entity keys (the source of truth for prereq-key matching), without
    # editing any tracked file. Also surface the LLM concept-tag response.
    import apollo.provisioning.orchestrator as _orch
    from apollo.persistence.learner_model_seed import reference_solution_to_entities

    _orig_tag = _orch.tag_and_mint
    _orig_chat_factory = _orch._tag_mint_chat_fn

    def _traced_chat_factory(metered_chat):
        inner = _orig_chat_factory(metered_chat)

        def _wrapped(prompt: str) -> str:
            out = inner(prompt)
            print(f"[DBG] concept_tag LLM response: {out!r}")
            return out

        return _wrapped

    async def _traced_tag(db, pair, **kw):
        try:
            keys = [s.canonical_key for s in reference_solution_to_entities(pair.problem)]
            print(f"[DBG] minted entity keys (from reference_solution): {keys}")
        except Exception as ee:  # noqa: BLE001
            print(f"[DBG] could not derive minted keys: {ee!r}")
        try:
            return await _orig_tag(db, pair, **kw)
        except Exception as e:  # noqa: BLE001
            print(f"[DBG] TAGMINT_FAIL type={type(e).__name__} msg={e}")
            raise

    _orch.tag_and_mint = _traced_tag
    _orch._tag_mint_chat_fn = _traced_chat_factory

    neo = Neo4jClient.from_env()
    try:
        outcome = await _drain_one(
            neo,
            session_factory=get_async_session,
            metered_chat_factory=_default_metered_factory,
        )
    finally:
        await neo.close()

    if outcome is None:
        print("RESULT: NO_JOB_CLAIMED (queue empty)")
        return 1
    print(
        "RESULT: "
        f"status={outcome.status} run_id={outcome.run_id} "
        f"scraped={outcome.n_questions_scraped} promoted={outcome.n_promoted} "
        f"rejected={outcome.n_rejected} merged={outcome.n_dedup_merged}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
