"""Pure resolver: reference-node node_id -> opposing bank code (F-struct).

Keeps ``gate.py`` pure and node-shape-agnostic. The caller (done.py / the
campaign harness) has both the reference graph and the loaded bank in hand — the
same place ``centrality`` is computed — so it pre-resolves each bank entry's
``opposes`` (an ``entity_key``) to the reference node carrying that key, then
hands the gate a ``node_id``-keyed map matching the gate's own ``concept_key``
keying.

Design decision D3 (multi-opposes): if >1 bank entry opposes the SAME node, the
lexicographically-lowest ``code`` wins (deterministic; the labeled cluster is
1:1, so this only guards nondeterminism). No IO, no LLM, no DB.
"""

from __future__ import annotations

from apollo.ontology import KGGraph
from apollo.overseer.misconception_bank import MisconceptionEntry


def build_opposes_index(
    reference_graph: KGGraph,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> dict[str, str]:
    """Return ``{node_id: bank_code}`` for every reference node whose
    ``entity_key`` is opposed by at least one bank entry. On a tie, the lowest
    ``code`` wins. Nodes without an ``entity_key`` and entries without
    ``opposes`` contribute nothing."""
    key_to_node_id: dict[str, str] = {
        n.entity_key: n.node_id for n in reference_graph.nodes if n.entity_key
    }
    index: dict[str, str] = {}
    for entry in bank_entries:
        if not entry.opposes:
            continue
        node_id = key_to_node_id.get(entry.opposes)
        if node_id is None:
            continue
        existing = index.get(node_id)
        if existing is None or entry.code < existing:
            index[node_id] = entry.code
    return index


__all__ = ["build_opposes_index"]
