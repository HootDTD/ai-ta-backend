"""Semantic shortlist — retriever tier for the NLI adjudicator.

Given a student node and a tuple of reference Candidates, proposes the top-K
most similar candidates by embedding cosine (when an embedder is supplied) or
lexical token-overlap (fallback when no embedder).

This module is a RETRIEVER: it narrows the candidate set but NEVER resolves a
match.  A later tier (NLI adjudicator) decides whether the shortlisted
candidate is actually a match.  Caller pre-filters for type-compatibility and
misconception exclusion before calling this function.
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.embedding import (
    CandidateEmbeddingCache,
    Embedder,
    candidate_surface_texts,
    cosine,
)
from apollo.resolution.tiers import student_surface_text


@dataclass(frozen=True)
class SemanticCandidate:
    """One shortlisted candidate with its similarity score and provenance.

    ``source`` is ``"lexical"`` when token-overlap was used (no embedder) or
    ``"embedding"`` when cosine similarity was used.
    """

    candidate: Candidate
    text: str  # the specific surface text that produced the best score
    score: float
    source: str  # "lexical" | "embedding"


def _overlap(a: str, b: str) -> float:
    """Jaccard token-overlap between two strings (order-insensitive, lowercased,
    punctuation-stripped).  Returns 0.0 when either set is empty."""
    sa = {w.strip(".,;:!?").lower() for w in a.split()}
    sb = {w.strip(".,;:!?").lower() for w in b.split()}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def shortlist_semantic_candidates(
    student_node: Node,
    candidates: tuple[Candidate, ...],
    *,
    top_k: int = 5,
    embedder: Embedder | None = None,
    cache: CandidateEmbeddingCache | None = None,
) -> list[SemanticCandidate]:
    """Return the top-K candidates most similar to ``student_node``.

    When ``embedder`` is *None* the similarity is Jaccard token-overlap over
    the student surface text vs every candidate surface text (from
    :func:`~apollo.resolution.embedding.candidate_surface_texts`).

    When ``embedder`` is provided it is called to produce cosine-similarity
    scores.  ``cache`` is an optional :class:`CandidateEmbeddingCache` that
    amortises re-embedding the same candidate set across multiple student
    nodes in one turn; a fresh cache is created when *None*.

    Results are ranked ``(-score, canonical_key)`` so equal scores produce a
    deterministic, alphabetical order.  Returns at most ``top_k`` items.
    """
    text = student_surface_text(student_node)
    if not text or not candidates:
        return []

    scored: list[SemanticCandidate] = []

    if embedder is None:
        for c in candidates:
            best_text, best = "", 0.0
            for surf in candidate_surface_texts(c):
                s = _overlap(text, surf)
                if s >= best:
                    best, best_text = s, surf
            scored.append(SemanticCandidate(c, best_text, best, "lexical"))
    else:
        sv = embedder([text])[0]
        vecs = (cache or CandidateEmbeddingCache()).vectors_for(candidates, embedder=embedder)
        for c in candidates:
            best_text, best = "", 0.0
            surfaces = candidate_surface_texts(c)
            for surf, v in zip(surfaces, vecs.get(c.canonical_key, []), strict=False):
                s = cosine(sv, v)
                if s >= best:
                    best, best_text = s, surf
            scored.append(SemanticCandidate(c, best_text, best, "embedding"))

    scored.sort(key=lambda sc: (-sc.score, sc.candidate.canonical_key))
    return scored[:top_k]
