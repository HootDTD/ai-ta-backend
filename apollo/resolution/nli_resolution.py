from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apollo.ontology.nodes import Node
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, NLI_NODE_TYPES, Candidate
from apollo.resolution.embedding import CandidateEmbeddingCache, Embedder
from apollo.resolution.nli_adjudicator import NLIAdjudicator, NLIResult
from apollo.resolution.nli_config import NLIParams
from apollo.resolution.polarity import polarity_allows_match
from apollo.resolution.semantic_shortlist import SemanticCandidate, shortlist_semantic_candidates
from apollo.resolution.structural import ScoredMatch
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class NLIContext:
    nli: NLIAdjudicator | None = None
    embedder: Embedder | None = None
    cache: CandidateEmbeddingCache | None = None
    params: NLIParams = field(default_factory=NLIParams)


def _content_tokens(t: str) -> set[str]:
    return {w.strip(".,;:!?").lower() for w in t.split() if len(w) > 2}


def match_nli_semantic(
    student_node: Node, type_ok: tuple[Candidate, ...], *, ctx: NLIContext
) -> ScoredMatch | None:
    if ctx.nli is None:
        return None
    text = student_surface_text(student_node)
    if not text or student_node.node_type not in NLI_NODE_TYPES:
        return None
    p = ctx.params
    refs = tuple(c for c in type_ok if not c.is_misconception)
    miscs = tuple(c for c in type_ok if c.is_misconception)

    # --- Semantic veto: student voicing a (paraphrased) misconception? ---
    # Flag OFF (default): VETO-ONLY — reference credit is blocked (return None)
    # but the node never resolves TO the misconception (control issue #82:
    # nothing ever certified to a misc.* key, so paraphrased misconceptions
    # were undetectable). Flag ON (APOLLO_NLI_MISC_POSITIVE_CERTIFY): the same
    # entailment >= misconception_veto_entailment POSITIVELY resolves the node
    # to the misc.* candidate, symmetric to the reference certify below (same
    # ScoredMatch shape, method "nli", cap 0.88). The veto side-effect is
    # preserved in both states: reference credit is blocked either way.
    for sc in shortlist_semantic_candidates(
        student_node, miscs, top_k=p.top_k, embedder=ctx.embedder, cache=ctx.cache
    ):
        if not polarity_allows_match(text, sc.text).allowed:
            continue
        r = ctx.nli.classify(premise=text, hypothesis=sc.text)
        if r.label == "entailment" and r.entailment >= p.misconception_veto_entailment:
            if p.misc_positive_certify:
                _LOG.info(
                    "nli_misconception_certify key=%s ent=%.3f",
                    sc.candidate.canonical_key,
                    r.entailment,
                )
                return ScoredMatch(
                    student_node.node_id, sc.candidate, "nli", METHOD_CONFIDENCE_CAP["nli"]
                )
            _LOG.info(
                "nli_misconception_veto key=%s ent=%.3f", sc.candidate.canonical_key, r.entailment
            )
            return None

    # --- Certify references ---
    passed: list[tuple[SemanticCandidate, NLIResult]] = []
    for sc in shortlist_semantic_candidates(
        student_node, refs, top_k=p.top_k, embedder=ctx.embedder, cache=ctx.cache
    ):
        if not polarity_allows_match(text, sc.text).allowed:
            continue
        if not (
            _content_tokens(text) & _content_tokens(sc.text)
        ):  # positive-overlap floor (review B6)
            continue
        r = ctx.nli.classify(premise=text, hypothesis=sc.text)
        if (
            r.label == "entailment"
            and r.entailment >= p.min_entailment
            and r.contradiction <= p.max_contradiction
        ):
            passed.append((sc, r))
        elif r.contradiction > p.max_contradiction:
            _LOG.info(
                "nli_contradiction_signal key=%s c=%.3f",
                sc.candidate.canonical_key,
                r.contradiction,
            )

    if not passed:
        return None
    passed.sort(key=lambda pr: (-pr[1].entailment, pr[0].candidate.canonical_key))
    if (
        len(passed) >= 2
        and (passed[0][1].entailment - passed[1][1].entailment) < p.ambiguity_margin
    ):
        _LOG.info("nli_ambiguous top2=%.3f,%.3f", passed[0][1].entailment, passed[1][1].entailment)
        return None
    sc, _ = passed[0]
    return ScoredMatch(student_node.node_id, sc.candidate, "nli", METHOD_CONFIDENCE_CAP["nli"])
