"""WU-4B3 §2 — reference_graph_hash: a STABLE fingerprint of R_norm AS GRADED.

The persisted ``apollo_graph_comparison_runs.reference_graph_hash`` records WHICH
reference graph a run was graded against, so:
  * old runs stay EXPLAINABLE (replaying the same graph reproduces the hash), and
  * a teacher edit to the reference CHANGES the hash (a new grading regime).

Determinism is load-bearing. We do NOT use builtin ``hash()`` or ``repr`` — both
vary across processes (``PYTHONHASHSEED``) and dataclass field reordering.
Instead we build a SORTED-CANONICAL payload and ``sha256`` a stable
``json.dumps(sort_keys=True)`` of it. The digest is prefixed with a version tag
so a future serialization change is self-describing.

Identity rules (what the hash IS sensitive to):
  * nodes: ``(canonical_key, node_type, symbolic)`` — the comparison identity +
    the symbolic surface for equations. The reference step ids
    (``source_node_ids``) and ``evidence_spans`` are EXCLUDED: renaming a step id
    without changing the graph SHAPE must keep the hash stable.
  * edges: ``(edge_type, from_key, to_key)``.
  * paths: each path's ``canonical_keys`` tuple.
Each list is SORTED so tuple/dict construction order never changes the hash.

Pure + immutable: reads a frozen ``ReferenceGraph``, returns a string.
"""

from __future__ import annotations

import hashlib
import json

from apollo.graph_compare.canonical import ReferenceGraph

# Bumped if the serialization below changes, so a stored hash is self-describing
# (a ``refhash-v2:`` digest is known to use a different payload shape).
REFERENCE_HASH_VERSION: str = "refhash-v1"


def reference_graph_hash(reference_graph: ReferenceGraph) -> str:
    """A deterministic version-prefixed sha256 over the sorted-canonical
    ``ReferenceGraph`` (nodes key+type+symbolic, edges, path key-tuples).

    Stable across replays; changes when any node / edge / path changes. Returns
    ``f"{REFERENCE_HASH_VERSION}:{hexdigest}"``."""
    payload = {
        "nodes": sorted(
            [n.canonical_key, str(n.node_type), n.symbolic or ""]
            for n in reference_graph.nodes
        ),
        "edges": sorted(
            [str(e.edge_type), e.from_key, e.to_key] for e in reference_graph.edges
        ),
        "paths": sorted(list(p.canonical_keys) for p in reference_graph.paths),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{REFERENCE_HASH_VERSION}:{digest}"
