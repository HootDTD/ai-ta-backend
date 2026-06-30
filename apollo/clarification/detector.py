"""Embedding-similarity detector — flags residual student nodes that are
plausibly (not confidently) a candidate idea, for an answer-blind follow-up.
High recall by design: a false positive costs only a question the student
dismisses. It NEVER credits (spec §4)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from apollo.clarification.embedding import CandidateEmbeddingCache, Embedder, cosine
from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)

T_AMBIG = 0.50  # calibration default (spec §15); recall-tuned, tolerant precision.


@dataclass(frozen=True)
class FlaggedNode:
    node: Node
    candidate: Candidate
    cosine: float


def detect_ambiguous_nodes(
    residual_nodes: list[Node],
    candidates: tuple[Candidate, ...],
    *,
    embedder: Embedder,
    cache: CandidateEmbeddingCache,
    t_ambig: float = T_AMBIG,
) -> list[FlaggedNode]:
    """For each residual node, the top candidate by max cosine over its surface
    forms; flag when cosine >= t_ambig. Fail-safe: empty list on any embedder
    error (the turn proceeds with no probe)."""
    if not residual_nodes or not candidates:
        return []
    try:
        cand_vectors = cache.vectors_for(candidates, embedder=embedder)
        texts = [student_surface_text(n) for n in residual_nodes]
        node_vectors = embedder(texts)
    except Exception as exc:  # noqa: BLE001 - fail safe, never block teaching
        _LOG.warning("clarification_detect_embed_failed error=%s", exc)
        return []

    by_key = {c.canonical_key: c for c in candidates}
    flagged: list[FlaggedNode] = []
    for node, nvec in zip(residual_nodes, node_vectors):
        best_key, best_cos = None, -1.0
        for key, surfaces in cand_vectors.items():
            for svec in surfaces:
                c = cosine(nvec, svec)
                if c > best_cos:
                    best_cos, best_key = c, key
        if best_key is not None and best_cos >= t_ambig:
            flagged.append(FlaggedNode(node=node, candidate=by_key[best_key], cosine=best_cos))
    return flagged
