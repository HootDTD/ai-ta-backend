"""Apollo §8B auto-provisioning promotion-lint package (WU-3B2b).

The SAFETY CORE of the auto-provisioning pipeline: before any auto-scraped
problem is promoted Tier-1 -> Tier-2 (teachable), it must pass the eight §8B.4
gates run IN ORDER, short-circuiting on the first failure. This package is PURE
by design — NO LLM, NO DB, NO Neo4j, NO containers, NO migration:

  * gate-4 ``canonical_symbols`` / ``normalization_map`` and gate-8
    ``existing_problem_hashes`` are PASSED IN by the caller (populated later by
    3B2a / 3B2d), so the core never touches the database;
  * it owns the gate logic + diagnostic ONLY — it does NOT promote, call
    ``project_canon``, or write ``apollo_rejected_problems`` (the
    ``PromotionResult`` -> promote/reject mapping is 3B2g's orchestrator).

Mirrors ``apollo/resolution/`` (standalone, flat re-export ``__init__``) so the
3B2g orchestrator imports it (``from apollo.provisioning import
run_promotion_lint, ...``) rather than owning it.
"""

from __future__ import annotations

from apollo.provisioning.cost_constants import (
    MAX_ATTEMPTS,
    MODEL_PRICES,
    PER_DOCUMENT_TOKEN_CEILING,
    cost_usd_for,
)
from apollo.provisioning.dedup import DedupVerdict, resolve_candidate
from apollo.provisioning.enqueue import enqueue_provisioning_job
from apollo.provisioning.ingest import (
    AuthoredProblem,
    IngestResult,
    ingest_authored_problems,
    load_authored_problems,
    write_authored_tier1_problems,
)
from apollo.provisioning.metered_chat import CostBudgetExceeded, MeteredChat
from apollo.provisioning.orchestrator import (
    AuthoredProvisionResult,
    ProvisioningOutcome,
    provision_authored_problem,
    run_provisioning,
)
from apollo.provisioning.pairing_gate import (
    PairingVerdict,
    Rejection,
    rejection_from_verdict,
    validate_pair,
)
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.problem_leak_guard import (
    ProblemLeakVerdict,
    check_problem_leak,
)
from apollo.provisioning.promote import PromoteResult, promote
from apollo.provisioning.promotion_lint import PromotionResult, run_promotion_lint
from apollo.provisioning.queue import (
    ClaimedJob,
    claim_provisioning_job,
    complete_job,
    fail_job,
    release_job,
)
from apollo.provisioning.scrape import (
    CandidateQuestion,
    ScrapeResult,
    scrape_document,
    scrape_questions,
    write_tier1_problems,
)
from apollo.provisioning.solution import (
    GroundingSpan,
    ReferenceSolutionDraft,
    SolutionDraftError,
    build_approved_pair,
    build_authored_approved_pair,
    construct_authored_reference,
    find_or_generate,
    solution_hash,
)
from apollo.provisioning.tag_mint import (
    ApprovedPair,
    MintPlan,
    TagMintError,
    tag_and_mint,
)

__all__ = [
    "PromotionResult",
    "run_promotion_lint",
    "problem_dup_hash",
    # GEN-1 — standalone generated-problem answer-leak guard
    "ProblemLeakVerdict",
    "check_problem_leak",
    "DedupVerdict",
    "resolve_candidate",
    # WU-3B2d — scrape (stage 1) public surface
    "CandidateQuestion",
    "ScrapeResult",
    "scrape_document",
    "scrape_questions",
    "write_tier1_problems",
    # WU-3B2d — tag/mint (stage 4) public surface
    "ApprovedPair",
    "MintPlan",
    "TagMintError",
    "tag_and_mint",
    # WU-3B2e — find-or-generate (stage 2) public surface
    "GroundingSpan",
    "ReferenceSolutionDraft",
    "SolutionDraftError",
    "find_or_generate",
    "solution_hash",
    "build_approved_pair",
    # Subject-fluid Apollo — authored construction (solution.py)
    "construct_authored_reference",
    "build_authored_approved_pair",
    # Subject-fluid Apollo — authored per-candidate pipeline (orchestrator.py)
    "provision_authored_problem",
    "AuthoredProvisionResult",
    # WU-3B2e — pairing/correctness gate (stage 3) public surface
    "PairingVerdict",
    "Rejection",
    "validate_pair",
    "rejection_from_verdict",
    # WU-3B2f — SKIP-LOCKED claim/lease queue drain (queue.py)
    "ClaimedJob",
    "claim_provisioning_job",
    "complete_job",
    "fail_job",
    "release_job",
    # WU-3B2f — metered LLM client (metered_chat.py)
    "MeteredChat",
    "CostBudgetExceeded",
    # WU-3B2f — cost/attempt constants (cost_constants.py)
    "PER_DOCUMENT_TOKEN_CEILING",
    "MAX_ATTEMPTS",
    "MODEL_PRICES",
    "cost_usd_for",
    # WU-3B2g — enqueue seam (enqueue.py)
    "enqueue_provisioning_job",
    # WU-3B2g — promotion step (promote.py)
    "promote",
    "PromoteResult",
    # WU-3B2g — orchestrator (orchestrator.py)
    "run_provisioning",
    "ProvisioningOutcome",
    # Subject-agnostic Apollo — authored-problem ingest (ingest.py)
    "AuthoredProblem",
    "IngestResult",
    "ingest_authored_problems",
    "load_authored_problems",
    "write_authored_tier1_problems",
]
