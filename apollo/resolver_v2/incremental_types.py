"""Resolver V2 incremental scoring types (spec §2.1, §5.2, §3.1).

Frozen dataclasses used by the per-turn incremental scorer
(``apollo/resolver_v2/incremental.py``, T-later) to carry state across chat
turns and to hand a per-turn output snapshot to the clarification-v2 ranker.

Kept separate from ``types.py`` to keep that module <400 lines (per the
design's file-layout table, §2.1); may be folded back into ``types.py`` if it
stays small.

Pure module: stdlib only, no DB, no env reads (resolver_v2 purity, §13).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class IncrementalState:
    """Persisted per-attempt incremental scoring state (design §5.2).

    Immutable: every per-turn update (``incremental.score_turn``) returns a
    **new** ``IncrementalState`` instance — never mutates this one in place.
    """

    window_cursor: int  # number of student turns already windowed
    global_window_count: int  # windows emitted so far (global index offset, §5.3 1a)
    running_node_max: Mapping[str, float]  # canonical_key -> best fused score so far
    node_source: Mapping[str, str]  # canonical_key -> source of that best
    # edge_key "TYPE|from|to" -> best relation-evidence tier seen so far
    # (A-MAJOR-1: monotone r(e))
    running_edge_evidence: Mapping[str, str]
    seeded_keys: frozenset[str]  # keys pinned by clarification (§6)
    pair_count_total: int  # cumulative NLI pairs used (budget guard)


@dataclass(frozen=True)
class IncrementalSnapshot:
    """One turn's incremental scoring output (design §3.1 output line).

    Not the grading source of truth (§3.2) — a conservative monotone lower
    bound on the from-scratch batch grade (§5.4), used only to rank
    clarification questions.
    """

    node_credits: Mapping[str, float]  # canonical_key -> running credit
    edge_scores: tuple  # per-edge scored data, in reference order
    node_cov: float  # winning-path mean node credit (running)
    edge_cov: float  # mean edge credit over all reference edges (running)
    winning_path_index: int
    gray: frozenset[str]  # canonical_keys currently classified gray
    pair_count_this_turn: int  # NLI pairs spent this turn (budget audit)
