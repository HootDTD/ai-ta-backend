"""Resolver V2 shared types — the T1 interlock contract (design §5.0).

Frozen dataclasses copied VERBATIM from
``docs/_archive/specs/2026-07-07-resolver-v2-design.md`` §5.0. Every other
resolver_v2 module (windows/prefilter T3, scoring T4, edges/aggregate T5,
grayzone T6, engine T7) imports these — do not change field names, order, or
semantics without updating the design card.

Pure module: stdlib only, no heavy imports (no transformers, no DB).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Window:
    """One student-transcript premise window (design §5.1)."""

    index: int  # 0-based transcript order
    turn_index: int  # source student-turn ordinal
    text: str  # premise text, <= max_window_words


@dataclass(frozen=True)
class RefNode:
    """One reference node to score, with its affirmative hypothesis views
    (design §5.2). ``views[0]`` is ALWAYS the payload ``content.label``."""

    canonical_key: str
    node_type: str  # ontology NodeType string
    label: str  # problem step content.label (fallback: canonical_key)
    views: tuple[str, ...]  # affirmative views; views[0] == label ALWAYS


@dataclass(frozen=True)
class PairScore:
    """Score of one (window, view) NLI/lexical pair (design §5.4)."""

    window_index: int
    view_index: int
    lexical: float  # [0,1]
    entailment: float  # [0,1]; 0.0 when NLI skipped
    contradiction: float  # [0,1]
    fused: float  # §5.4 fusion


@dataclass(frozen=True)
class NodeScore:
    """Final per-reference-node score + graded credit (design §5.4-§5.5)."""

    canonical_key: str
    score: float  # max fused over pairs
    credit: float  # g(score) after grayzone + floors, in [0,1]
    source: str  # "nli"|"lexical_skip"|"v1_floor"|"grayzone"|"edge_pullup"|"zero"
    best: PairScore | None  # argmax pair (None when skipped)


@dataclass(frozen=True)
class EdgeScore:
    """Graded credit for one reference edge (design §5.6)."""

    edge_type: str  # "USES"|"DEPENDS_ON"|"SCOPES"|"PRECEDES"
    from_key: str
    to_key: str
    credit: float  # [0,1]
    relation_evidence: str  # "entail"|"cooccur"|"endpoints"|"v1_explicit"|"v1_inferred"|"none"


@dataclass(frozen=True)
class ResolverV2Result:
    """The engine's full output (design §5.0). ``node_coverage`` /
    ``edge_coverage`` are the exact numbers substituted into ``GradeResult``
    when the flag is on (§2 scope guards)."""

    node_scores: tuple[NodeScore, ...]  # one per distinct ref key (union over paths), key-sorted
    edge_scores: tuple[EdgeScore, ...]  # one per reference edge, in reference order
    node_coverage: float  # winning-path mean credit
    edge_coverage: float  # mean edge credit over ALL reference edges
    winning_path_index: int
    grayzone_used: bool
    pair_count: int  # NLI pairs actually run (budget audit)

    def trace(self) -> dict:
        """JSON-safe trace dict: ``{"summary": {...}, "nodes": [...],
        "edges": [...]}``. The ``summary`` block is what
        ``artifact_build.py`` nests under ``scores.resolver_v2`` (T7);
        ``nodes``/``edges`` are the full per-item audit (calibration input
        when ``APOLLO_RESOLVER_V2_TRACE_DIR`` is set). Must survive a
        ``json.dumps`` round-trip."""
        return {
            "summary": {
                "node_coverage": self.node_coverage,
                "edge_coverage": self.edge_coverage,
                "winning_path_index": self.winning_path_index,
                "grayzone_used": self.grayzone_used,
                "pair_count": self.pair_count,
                "node_count": len(self.node_scores),
                "edge_count": len(self.edge_scores),
            },
            "nodes": [asdict(node) for node in self.node_scores],
            "edges": [asdict(edge) for edge in self.edge_scores],
        }


#: Type-selector callable (breaks the T3<->T4/T5 import dependency; tests
#: inject fakes): (windows, view_text, k) -> ((window_index, lexical_score),
#: ...) top-k, ties -> lowest index.
SelectFn = Callable[[Sequence[Window], str, int], tuple[tuple[int, float], ...]]
