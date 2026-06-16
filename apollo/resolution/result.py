"""WU-3C2 — the resolver's structured result model (§5).

``ResolutionResult`` is the resolver's return value: one ``ResolvedNode`` per
student evidence node (including the unresolved ones — a non-match is DATA, not
an error), a per-method tier histogram (§6.7 tier distribution), and the LLM
call count (which MUST be <= 1: one adjudication per attempt max).

Persistence (``RESOLVES_TO`` edges + the four resolution node-fields) is a
DIFFERENT, lower-coupling concern that lives in
``apollo.knowledge_graph.resolution_store``; that module imports this result and
maps it to its Neo4j specs. Keeping the spec types out of ``result.py`` avoids a
circular import (result is the lower-level seam WU-4A also imports directly).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ResolvedNode:
    """The resolution outcome for one student evidence node. Immutable.

    ``resolution`` is ``'resolved' | 'unresolved' | 'ambiguous'``;
    ``resolved_key`` is the matched candidate's ``canonical_key`` (None when
    unresolved); ``resolved_canon_key`` is the ``:Canon`` surrogate key for the
    ``RESOLVES_TO`` edge (None when unresolved or when the candidate has no
    projected ``:Canon`` node); ``confidence`` is already capped by
    ``METHOD_CONFIDENCE_CAP[method]``.
    """

    node_id: str
    resolution: str
    resolved_key: str | None
    resolved_canon_key: int | None
    method: str
    confidence: float


@dataclass(frozen=True)
class ResolutionResult:
    """Structured outcome of one ``resolve_attempt`` run. Immutable.

    ``resolved`` carries one entry per student node (unresolved included);
    ``tier_counts`` is the per-method histogram; ``llm_calls`` is the number of
    LLM adjudication calls made (binding: <= 1)."""

    resolved: tuple[ResolvedNode, ...]
    tier_counts: Mapping[str, int]
    llm_calls: int

    def resolved_edges(self) -> tuple[ResolvedNode, ...]:
        """The subset of resolved nodes that can become a ``RESOLVES_TO`` edge:
        ``resolution == 'resolved'`` AND a non-null ``resolved_canon_key``.

        Unresolved nodes (no edge — DATA) and resolved-but-unprojected
        candidates (no ``:Canon`` target yet) are excluded."""
        return tuple(
            rn
            for rn in self.resolved
            if rn.resolution == "resolved" and rn.resolved_canon_key is not None
        )
