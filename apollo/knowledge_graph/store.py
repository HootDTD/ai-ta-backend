"""Knowledge Graph store — Neo4j-backed CRUD + freeze enforcement.

V3 contract:
- Per-attempt subgraphs in Neo4j, scoped by `attempt_id` property + secondary
  `:_KGNode` label.
- All reads/writes go through `KGGraph` (apollo.ontology.graph), the single
  source-of-truth shape.
- Postgres still owns session phase (freeze/unfreeze).
- `delete_subgraph` is the cross-DB cleanup contract called by handlers in
  try/finally on session-end. Idempotent.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sympy import latex

from apollo.errors import SessionFrozenError
from apollo.ontology import (
    NODE_CONTENT_TYPES,
    NODE_LABEL_TO_TYPE,
    NODE_LABELS,
    Edge,
    EdgeType,
    KGGraph,
    Node,
    NodeType,
    build_node,
)
from apollo.persistence.models import ApolloSession, ProblemAttempt
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.solver.sympy_exec import _tidy_floats, parse_zero_form

_LOG = logging.getLogger(__name__)
_EMPTY_SUMMARY = "(the student hasn't taught me anything yet)"


# ---------------------------------------------------------------------------
# Cypher templates — one CREATE per node label (Cypher does not allow dynamic
# labels). Templates apply both the type label AND the secondary :_KGNode
# label so a single index covers all subgraph reads.
# ---------------------------------------------------------------------------

_NODE_CREATE_CYPHER: dict[str, str] = {
    label: (
        f"UNWIND $rows AS row\n"
        f"CREATE (n:{label}:_KGNode)\n"
        f"SET n = row\n"
        f"RETURN count(n) AS written"
    )
    for label in NODE_LABELS.values()
}

_EDGE_CREATE_CYPHER: dict[str, str] = {
    et.value: (
        f"UNWIND $rows AS row\n"
        f"MATCH (a:_KGNode {{attempt_id: row.attempt_id, node_id: row.from_node_id}})\n"
        f"MATCH (b:_KGNode {{attempt_id: row.attempt_id, node_id: row.to_node_id}})\n"
        f"CREATE (a)-[e:{et.value} {{attempt_id: row.attempt_id, source: row.source}}]->(b)\n"
        f"RETURN count(e) AS written"
    )
    for et in EdgeType
}


def _equation_latex(symbolic: str) -> str | None:
    """Best-effort LaTeX render for display. Returns None on parse failure."""
    try:
        expr = parse_zero_form(symbolic, entry_id="_display_only")
        return latex(_tidy_floats(expr))
    except Exception:  # noqa: BLE001
        return None


def _node_to_neo4j_props(node: Node) -> dict[str, Any]:
    """Flatten a typed Node into a Neo4j property bag."""
    content = node.content.model_dump()
    return {
        "node_id": node.node_id,
        "attempt_id": node.attempt_id,
        "source": node.source,
        **content,
    }


def _record_to_node(props: dict[str, Any], labels: list[str]) -> Node:
    """Reconstruct a typed Node from a Neo4j record."""
    label_set = set(labels) - {"_KGNode"}
    if not label_set:
        raise ValueError(f"node has no type label: labels={labels}")
    if len(label_set) > 1:
        raise ValueError(f"node has multiple type labels: {labels}")
    label = next(iter(label_set))
    if label not in NODE_LABEL_TO_TYPE:
        raise ValueError(f"unknown node label: {label}")

    node_type: NodeType = NODE_LABEL_TO_TYPE[label]
    bag = dict(props)
    node_id = bag.pop("node_id")
    attempt_id = bag.pop("attempt_id")
    source = bag.pop("source")

    if node_type == "equation" and "symbolic" in bag and "latex" not in bag:
        tex = _equation_latex(bag["symbolic"])
        if tex is not None:
            bag["latex"] = tex

    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=int(attempt_id),
        source=source,
        content=bag,
    )


def _edge_to_neo4j_row(edge: Edge) -> dict[str, Any]:
    return {
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "attempt_id": edge.attempt_id,
        "source": edge.source,
    }


# ---------------------------------------------------------------------------
# KGStore
# ---------------------------------------------------------------------------

class KGStore:
    """Per-request store. Postgres for session/freeze state, Neo4j for graph."""

    def __init__(self, db: AsyncSession, neo: Neo4jClient) -> None:
        self.db = db
        self.neo = neo

    # ------- Postgres-backed freeze / metadata ------------------------------

    async def _session_id_for_attempt(self, attempt_id: int) -> int:
        row = await self.db.execute(
            select(ProblemAttempt.session_id).where(ProblemAttempt.id == attempt_id)
        )
        sid = row.scalar_one_or_none()
        if sid is None:
            raise ValueError(f"attempt {attempt_id} not found")
        return sid

    async def _ensure_unfrozen(self, session_id: int) -> None:
        result = await self.db.execute(
            select(ApolloSession.phase).where(ApolloSession.id == session_id)
        )
        phase = result.scalar_one_or_none()
        if phase in ("PROBLEM_REVEAL", "SOLVING", "REPORT"):
            raise SessionFrozenError(session_id=str(session_id))

    async def freeze(self, session_id: int) -> None:
        result = await self.db.execute(
            select(ApolloSession).where(ApolloSession.id == session_id)
        )
        session = result.scalar_one()
        session.phase = "PROBLEM_REVEAL"
        await self.db.commit()

    async def unfreeze(self, session_id: int) -> None:
        result = await self.db.execute(
            select(ApolloSession).where(ApolloSession.id == session_id)
        )
        session = result.scalar_one()
        session.phase = "TEACHING"
        await self.db.commit()

    # ------- Neo4j-backed CRUD ----------------------------------------------

    async def write_nodes(
        self,
        *,
        attempt_id: int,
        nodes: list[Node],
        source: str,
    ) -> int:
        """Create typed nodes in the per-attempt subgraph.

        `source` is set on each node before persistence so all written nodes
        share a provenance tag for this batch.
        """
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)
        if not nodes:
            return 0

        # Group by label for the per-label CREATE templates.
        rows_by_label: dict[str, list[dict[str, Any]]] = {}
        for n in nodes:
            label = NODE_LABELS[n.node_type]
            row = _node_to_neo4j_props(n)
            row["source"] = source
            row["attempt_id"] = attempt_id
            rows_by_label.setdefault(label, []).append(row)

        total = 0
        async with self.neo.session() as s:
            for label, rows in rows_by_label.items():
                cypher = _NODE_CREATE_CYPHER[label]
                result = await s.run(cypher, rows=rows)
                rec = await result.single()
                total += int(rec["written"]) if rec else 0
        return total

    async def write_edges(
        self,
        *,
        attempt_id: int,
        edges: list[Edge],
        source: str,
    ) -> int:
        """Create typed edges. Endpoints must already exist in the subgraph.

        Edges where MATCH fails to find both endpoints are silently dropped
        by Neo4j's `MATCH ... CREATE` semantics — the caller is responsible
        for validating endpoint existence (typically by writing nodes first).
        """
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)
        if not edges:
            return 0

        rows_by_type: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            row = _edge_to_neo4j_row(e)
            row["attempt_id"] = attempt_id
            row["source"] = source
            rows_by_type.setdefault(e.edge_type.value, []).append(row)

        total = 0
        async with self.neo.session() as s:
            for et, rows in rows_by_type.items():
                cypher = _EDGE_CREATE_CYPHER[et]
                result = await s.run(cypher, rows=rows)
                rec = await result.single()
                total += int(rec["written"]) if rec else 0
        return total

    async def read_graph(self, *, attempt_id: int) -> KGGraph:
        """Read the full per-attempt subgraph."""
        async with self.neo.session() as s:
            nodes_res = await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid}) "
                "RETURN n AS props, labels(n) AS labels",
                aid=attempt_id,
            )
            nodes: list[Node] = []
            async for record in nodes_res:
                props = dict(record["props"])
                labels = list(record["labels"])
                nodes.append(_record_to_node(props, labels))

            edges_res = await s.run(
                "MATCH (a:_KGNode {attempt_id: $aid})-[e]->(b:_KGNode {attempt_id: $aid}) "
                "RETURN type(e) AS edge_type, e.attempt_id AS attempt_id, "
                "e.source AS source, a.node_id AS from_id, b.node_id AS to_id, "
                "labels(a) AS from_labels, labels(b) AS to_labels",
                aid=attempt_id,
            )
            edges: list[Edge] = []
            async for record in edges_res:
                from_label_set = set(record["from_labels"]) - {"_KGNode"}
                to_label_set = set(record["to_labels"]) - {"_KGNode"}
                from_type = NODE_LABEL_TO_TYPE.get(next(iter(from_label_set), ""))
                to_type = NODE_LABEL_TO_TYPE.get(next(iter(to_label_set), ""))
                edges.append(
                    Edge(
                        edge_type=EdgeType(record["edge_type"]),
                        from_node_id=record["from_id"],
                        to_node_id=record["to_id"],
                        attempt_id=int(record["attempt_id"]),
                        source=record["source"] or "parser",
                        from_node_type=from_type,
                        to_node_type=to_type,
                    )
                )

        return KGGraph(nodes=nodes, edges=edges)

    async def walk_chain(
        self,
        *,
        attempt_id: int,
        start_node_id: str,
        edge_types: list[EdgeType],
        max_depth: int = 20,
    ) -> list[Node]:
        """Cypher-backed traversal from a start node along the given edge types."""
        if not edge_types:
            return []
        edge_pattern = "|".join(et.value for et in edge_types)
        cypher = (
            f"MATCH (start:_KGNode {{attempt_id: $aid, node_id: $sid}}) "
            f"MATCH (start)-[:{edge_pattern}*1..{max_depth}]->(n:_KGNode) "
            f"RETURN DISTINCT n AS props, labels(n) AS labels"
        )
        async with self.neo.session() as s:
            result = await s.run(cypher, aid=attempt_id, sid=start_node_id)
            out: list[Node] = []
            async for record in result:
                props = dict(record["props"])
                labels = list(record["labels"])
                out.append(_record_to_node(props, labels))
        return out

    async def delete_subgraph(self, *, attempt_id: int) -> None:
        """Idempotent cross-DB cleanup. Removes all nodes (and their edges)
        in the per-attempt subgraph. Safe to call when nothing exists."""
        async with self.neo.session() as s:
            await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid}) DETACH DELETE n",
                aid=attempt_id,
            )

    # ------- Apollo-facing summary ------------------------------------------

    async def summarize_for_apollo(self, *, attempt_id: int) -> str:
        """Bullet summary for Apollo's context. Format unchanged from V2 so
        Apollo's prompt does not need to relearn anything.

        Leakage contract: this summary is Apollo's vocabulary mirror — it
        deliberately exposes equation symbolics, variable mappings, and
        student framings verbatim. See `apollo/agent/LEAKAGE_POLICY.md` for
        the full policy. The downstream LLM-judge filter (item #3) evaluates
        Apollo's draft against:
          1. words the student typed (history),
          2. symbols and terms surfaced by this summary,
          3. concepts the student named explicitly.
        Apollo MAY reference (1)-(3); MUST NOT name un-introduced concepts.
        """
        graph = await self.read_graph(attempt_id=attempt_id)
        if not graph.nodes:
            return _EMPTY_SUMMARY

        lines: list[str] = []
        for n in graph.by_type("equation"):
            c = n.content
            label = getattr(c, "label", "") or "(no label)"
            lines.append(f"- equation ({label}): {c.symbolic}")
        for n in graph.by_type("definition"):
            c = n.content
            lines.append(f"- definition: {c.concept} = {c.meaning}")
        for n in graph.by_type("condition"):
            c = n.content
            lines.append(f"- condition: {c.applies_when}")
        for n in graph.by_type("simplification"):
            c = n.content
            lines.append(f"- simplification: when {c.applies_when}, {c.transformation}")
        for n in graph.by_type("variable_mapping"):
            c = n.content
            lines.append(f"- variable: {c.term} → {c.symbol}")

        # Procedure steps — derive ordering from PRECEDES chain
        chain = graph.precedes_chain()
        for i, n in enumerate(chain, start=1):
            c = n.content
            lines.append(f"- procedure step {i}: {c.action}")

        return "\n".join(lines) if lines else _EMPTY_SUMMARY
