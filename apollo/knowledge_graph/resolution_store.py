"""WU-3C2 — persistence for the §5 resolver: RESOLVES_TO edges + resolution fields.

Sibling of ``store.py`` (kept separate so neither file crosses the 800-line
ceiling). Writes:

- ``(:_KGNode {attempt_id, node_id})-[:RESOLVES_TO {method, confidence,
  resolved_at}]->(:Canon {key})`` — an idempotent ``MERGE`` (a re-run overwrites
  the three props, never duplicates the edge; mirrors the WU-3C1 ``:Canon``
  MERGE);
- the four Layer-2 resolution node-fields ``resolution`` / ``resolved_key`` /
  ``resolution_method`` / ``resolution_confidence`` on each ``:_KGNode``
  (idempotent ``SET``; ``resolved_key`` omitted when None per the None-omission
  convention).

``RESOLVES_TO`` is a ``:_KGNode -> :Canon`` CROSS-LABEL edge with a fixed shape,
written by its own Cypher here — it is deliberately NOT a member of
``EdgeType`` / ``EDGE_ALLOWED_PAIRS`` (those constrain within-subgraph
``:_KGNode -> :_KGNode`` edges written through ``store.write_edges``).

NO-FALLBACK: any Neo4j failure surfaces as ``ResolutionUnavailableError`` with
the offending ``stage``; the grade is already committed when resolution runs, so
the failure is loud but never voids it — the next Done / janitor retry re-runs
the idempotent writes (§5, §6.4).

Pure mapping seams (``resolved_node_to_edge_spec`` / ``resolved_node_to_field_spec``)
are DB-free so the spec shape is testable without a container.
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.errors import ResolutionUnavailableError
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.resolution.result import ResolutionResult, ResolvedNode

# ---------------------------------------------------------------------------
# Cypher — MERGE the cross-label edge on (student node, :Canon key); SET props.
# MERGE (not CREATE) is what makes re-resolution idempotent: same endpoints ->
# same edge, props overwritten.
# ---------------------------------------------------------------------------
_RESOLVES_TO_MERGE_CYPHER = (
    "UNWIND $rows AS row\n"
    "MATCH (n:_KGNode {attempt_id: $aid, node_id: row.node_id})\n"
    "MATCH (c:Canon {key: row.canon_key})\n"
    "MERGE (n)-[e:RESOLVES_TO]->(c)\n"
    "SET e.method = row.method,\n"
    "    e.confidence = row.confidence,\n"
    "    e.resolved_at = row.resolved_at\n"
    "RETURN count(e) AS written"
)

# Resolution node-fields. resolved_key uses coalesce so a None is written as a
# removal (REMOVE) rather than a literal "None" string — None-omission on read
# is preserved by store._record_to_node stripping the four props.
_RESOLUTION_FIELDS_CYPHER = (
    "UNWIND $rows AS row\n"
    "MATCH (n:_KGNode {attempt_id: $aid, node_id: row.node_id})\n"
    "SET n.resolution = row.resolution,\n"
    "    n.resolution_method = row.resolution_method,\n"
    "    n.resolution_confidence = row.resolution_confidence\n"
    "WITH n, row\n"
    "FOREACH (_ IN CASE WHEN row.resolved_key IS NULL THEN [] ELSE [1] END |\n"
    "    SET n.resolved_key = row.resolved_key)\n"
    "FOREACH (_ IN CASE WHEN row.resolved_key IS NULL THEN [1] ELSE [] END |\n"
    "    REMOVE n.resolved_key)\n"
    "RETURN count(n) AS written"
)


@dataclass(frozen=True)
class ResolvesToEdgeSpec:
    """Pre-Neo4j property bag for one ``RESOLVES_TO`` edge. Immutable."""

    node_id: str
    canon_key: int
    method: str
    confidence: float
    resolved_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class ResolutionFieldSpec:
    """Pre-Neo4j property bag for one node's four resolution fields. Immutable."""

    node_id: str
    resolution: str  # resolved | unresolved | ambiguous
    resolved_key: str | None
    resolution_method: str
    resolution_confidence: float


@dataclass(frozen=True)
class ResolutionWriteResult:
    """Structured outcome of :func:`write_resolution`."""

    edges: int
    fields: int


# ---------------------------------------------------------------------------
# Pure mapping seams (DB-free).
# ---------------------------------------------------------------------------


def resolved_node_to_edge_spec(rn: ResolvedNode, *, resolved_at: str) -> ResolvesToEdgeSpec | None:
    """A ``RESOLVES_TO`` edge spec for a resolved node that carries a ``:Canon``
    key, else None (unresolved nodes and resolved-but-unprojected candidates
    emit no edge — a non-edge is DATA)."""
    if rn.resolution != "resolved" or rn.resolved_canon_key is None:
        return None
    return ResolvesToEdgeSpec(
        node_id=rn.node_id,
        canon_key=rn.resolved_canon_key,
        method=rn.method,
        confidence=rn.confidence,
        resolved_at=resolved_at,
    )


def resolved_node_to_field_spec(rn: ResolvedNode) -> ResolutionFieldSpec:
    """The four resolution node-fields for EVERY node (unresolved included —
    the unresolved state is itself persisted data)."""
    return ResolutionFieldSpec(
        node_id=rn.node_id,
        resolution=rn.resolution,
        resolved_key=rn.resolved_key,
        resolution_method=rn.method,
        resolution_confidence=rn.confidence,
    )


def _edge_spec_to_row(spec: ResolvesToEdgeSpec) -> dict:
    return {
        "node_id": spec.node_id,
        "canon_key": spec.canon_key,
        "method": spec.method,
        "confidence": spec.confidence,
        "resolved_at": spec.resolved_at,
    }


def _field_spec_to_row(spec: ResolutionFieldSpec) -> dict:
    return {
        "node_id": spec.node_id,
        "resolution": spec.resolution,
        "resolved_key": spec.resolved_key,
        "resolution_method": spec.resolution_method,
        "resolution_confidence": spec.resolution_confidence,
    }


# ---------------------------------------------------------------------------
# Async write seams.
# ---------------------------------------------------------------------------


async def write_resolves_to(
    neo: Neo4jClient, attempt_id: int, specs: list[ResolvesToEdgeSpec]
) -> int:
    """Idempotently ``MERGE`` the ``RESOLVES_TO`` edges. Empty -> 0 without
    opening a session. Raises ``ResolutionUnavailableError(stage='write_resolves_to')``
    on any Neo4j failure (NO FALLBACK)."""
    if not specs:
        return 0
    rows = [_edge_spec_to_row(s) for s in specs]
    try:
        async with neo.session() as s:
            result = await s.run(_RESOLVES_TO_MERGE_CYPHER, aid=attempt_id, rows=rows)
            rec = await result.single()
            return int(rec["written"]) if rec else 0
    except ResolutionUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a named error
        raise ResolutionUnavailableError(stage="write_resolves_to", last_error=str(exc)) from exc


async def persist_resolution_fields(
    neo: Neo4jClient, attempt_id: int, specs: list[ResolutionFieldSpec]
) -> int:
    """Idempotently ``SET`` the four resolution node-fields. Empty -> 0 without
    opening a session. Raises ``ResolutionUnavailableError(stage='persist_fields')``
    on any Neo4j failure (NO FALLBACK)."""
    if not specs:
        return 0
    rows = [_field_spec_to_row(s) for s in specs]
    try:
        async with neo.session() as s:
            result = await s.run(_RESOLUTION_FIELDS_CYPHER, aid=attempt_id, rows=rows)
            rec = await result.single()
            return int(rec["written"]) if rec else 0
    except ResolutionUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a named error
        raise ResolutionUnavailableError(stage="persist_fields", last_error=str(exc)) from exc


async def write_resolution(
    neo: Neo4jClient,
    attempt_id: int,
    result: ResolutionResult,
    *,
    resolved_at: str,
) -> ResolutionWriteResult:
    """Convenience: write the RESOLVES_TO edges + resolution fields in two
    idempotent passes. Edges only for resolved nodes with a ``:Canon`` key;
    fields for every node (unresolved included)."""
    edge_specs = [
        spec
        for rn in result.resolved
        if (spec := resolved_node_to_edge_spec(rn, resolved_at=resolved_at)) is not None
    ]
    field_specs = [resolved_node_to_field_spec(rn) for rn in result.resolved]
    edges = await write_resolves_to(neo, attempt_id, edge_specs)
    fields = await persist_resolution_fields(neo, attempt_id, field_specs)
    return ResolutionWriteResult(edges=edges, fields=fields)
