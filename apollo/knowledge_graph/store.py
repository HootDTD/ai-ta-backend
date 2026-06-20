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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sympy import latex

from apollo.errors import KGEntryNotFoundError, RetentionError, SessionFrozenError
from apollo.ontology import (
    EDGE_ALLOWED_PAIRS,
    NODE_LABEL_TO_TYPE,
    NODE_LABELS,
    Edge,
    EdgeType,
    KGGraph,
    Node,
    NodeType,
    build_node,
)
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    Message,
    ProblemAttempt,
)
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.solver.sympy_exec import _tidy_floats, parse_zero_form

_LOG = logging.getLogger(__name__)
_EMPTY_SUMMARY = "(the student hasn't taught me anything yet)"


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp string for Layer-2 node metadata (created_at /
    graded_at). Neo4j has no native datetime in the existing string-prop
    convention; Δt-anchoring (§3) reads the stored value, never now()."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Cypher templates — one CREATE per node label (Cypher does not allow dynamic
# labels). Templates apply both the type label AND the secondary :_KGNode
# label so a single index covers all subgraph reads.
# ---------------------------------------------------------------------------

_NODE_CREATE_CYPHER: dict[str, str] = {
    label: (
        f"UNWIND $rows AS row\nCREATE (n:{label}:_KGNode)\nSET n = row\nRETURN count(n) AS written"
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
    """Flatten a typed Node into a Neo4j property bag.

    Negotiable-OLM fields (status, student_belief) ride on the node bag so
    the per-entry pill UI and the Done-gate can read them via the same
    `read_graph` call as everything else. Default ACCEPTED + null
    student_belief means write_nodes for parser-authored nodes is a no-op
    behavioral change vs pre-P3.
    """
    content = node.content.model_dump()
    return {
        "node_id": node.node_id,
        "attempt_id": node.attempt_id,
        "source": node.source,
        "parser_confidence": node.parser_confidence,
        "status": node.status,
        # Neo4j has no native NULL property — omit when None so reads see
        # a missing field rather than a literal "None" string.
        **({"student_belief": node.student_belief} if node.student_belief is not None else {}),
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
    # Default 1.0 keeps legacy nodes (written before P1) authoritative; the
    # P3 OLM Done-gate flags `parser_confidence < 0.6`, so legacy data does
    # not false-fire after the migration.
    parser_confidence = bag.pop("parser_confidence", 1.0)
    # P3: Negotiable OLM. Default ACCEPTED + None for nodes that pre-date
    # the P3 migration; coverage and the Done-gate treat that as the
    # pre-P3 baseline.
    status_raw = bag.pop("status", "ACCEPTED")
    student_belief = bag.pop("student_belief", None)
    # WU-3C1 Layer-2 scoping/timestamp metadata. These are NODE METADATA, not
    # node content — strip them before content reconstruction so read_graph
    # (and the FE-visible KG payload) round-trips byte-identically for scoped
    # nodes. Defaults None so pre-WU-3C1 nodes are unaffected.
    bag.pop("user_id", None)
    bag.pop("search_space_id", None)
    bag.pop("created_at", None)
    bag.pop("graded_at", None)

    if node_type == "equation" and "symbolic" in bag and "latex" not in bag:
        tex = _equation_latex(bag["symbolic"])
        if tex is not None:
            bag["latex"] = tex

    # Defensively coerce status — Neo4j returned a string; only the three
    # known values are meaningful. Anything else falls back to ACCEPTED so
    # a mis-set property doesn't crash the read path.
    status = status_raw if status_raw in ("ACCEPTED", "DISPUTED", "DUAL") else "ACCEPTED"

    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=int(attempt_id),
        source=source,
        content=bag,
        parser_confidence=float(parser_confidence),
        status=status,
        student_belief=student_belief,
    )


def _edge_to_neo4j_row(edge: Edge) -> dict[str, Any]:
    return {
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "attempt_id": edge.attempt_id,
        "source": edge.source,
    }


@dataclass(frozen=True)
class WriteEdgesResult:
    """Structured outcome of `KGStore.write_edges` (WU-2B).

    Replaces the bare `int` so the store distinguishes the two no-silent-drop
    cases: an endpoint absent from the subgraph (`dropped`) vs an edge whose
    endpoint types violate EDGE_ALLOWED_PAIRS (`invalid`). `reasons` carries
    `(edge_repr, reason)` for every rejected edge. `__int__` keeps the old
    contract loose for any caller that did `int(result)` (the sole non-test
    caller, `chat.py`, ignores the return).
    """

    written: int = 0
    dropped: int = 0  # endpoint(s) absent in the subgraph
    invalid: int = 0  # EDGE_ALLOWED_PAIRS / unknown-type violation
    reasons: tuple[tuple[str, str], ...] = ()  # (edge_repr, reason) per rejection

    def __int__(self) -> int:
        return self.written


def _edge_repr(edge: Edge) -> str:
    """Compact, log-safe edge identifier for rejection reasons."""
    return (
        f"{edge.edge_type.value}:{edge.from_node_id}->{edge.to_node_id}"
        f"({edge.from_node_type}->{edge.to_node_type})"
    )


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
        result = await self.db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
        session = result.scalar_one()
        session.phase = "PROBLEM_REVEAL"
        await self.db.commit()

    async def unfreeze(self, session_id: int) -> None:
        result = await self.db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
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
        user_id: str | None = None,
        search_space_id: int | None = None,
    ) -> int:
        """Create typed nodes in the per-attempt subgraph.

        `source` is set on each node before persistence so all written nodes
        share a provenance tag for this batch.

        Cross-turn de-dup (WU-2B): nodes whose id already exists in the
        subgraph are REUSED, not re-minted — re-CREATEing would clone a Neo4j
        node and unfairly inflate coverage. The existing ids are fetched once;
        only the genuinely new subset is CREATEd. The return value is the count
        of nodes ACTUALLY created (reused nodes are not counted as added — the
        `kg_entries_added` fair-coverage contract).

        WU-3C1 Layer-2 scoping (backward-compatible): the optional, keyword-only
        `user_id` (opaque UUID) and `search_space_id` are server-injected onto
        every created node's property bag; a `created_at` ISO-8601 UTC timestamp
        is ALWAYS stamped. Both scoping kwargs default `None` and follow the
        `None`-omission convention — omitted when None rather than written as a
        literal "None" — so existing callers are unchanged.
        """
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)
        if not nodes:
            return 0

        total = 0
        async with self.neo.session() as s:
            present = await self._existing_node_ids(
                s,
                attempt_id,
                [n.node_id for n in nodes],
            )
            to_create = [n for n in nodes if n.node_id not in present]
            reused = len(nodes) - len(to_create)

            # Group the survivors by label for the per-label CREATE templates.
            rows_by_label: dict[str, list[dict[str, Any]]] = {}
            created_at = _utc_now_iso()
            for n in to_create:
                label = NODE_LABELS[n.node_type]
                row = _node_to_neo4j_props(n)
                row["source"] = source
                row["attempt_id"] = attempt_id
                # WU-3C1 Layer-2 scoping/timestamp metadata. None-omission for
                # the scoping props; created_at is always present.
                if user_id is not None:
                    row["user_id"] = user_id
                if search_space_id is not None:
                    row["search_space_id"] = search_space_id
                row["created_at"] = created_at
                rows_by_label.setdefault(label, []).append(row)

            for label, rows in rows_by_label.items():
                cypher = _NODE_CREATE_CYPHER[label]
                result = await s.run(cypher, rows=rows)
                rec = await result.single()
                total += int(rec["written"]) if rec else 0

        _LOG.info(
            "write_nodes attempt_id=%s created=%s reused=%s",
            attempt_id,
            total,
            reused,
        )
        return total

    async def _existing_node_ids(
        self,
        s: Any,
        attempt_id: int,
        ids: Iterable[str],
    ) -> set[str]:
        """Return the subset of `ids` that already exist in the subgraph.

        One scoped read; safe (and Neo4j-free) on an empty id list. Runs inside
        the caller's `async with self.neo.session()` block so it shares the
        session with the surrounding write.
        """
        id_list = list(dict.fromkeys(ids))  # de-dup, preserve order
        if not id_list:
            return set()
        result = await s.run(
            "MATCH (n:_KGNode {attempt_id: $aid}) WHERE n.node_id IN $ids RETURN n.node_id AS id",
            aid=attempt_id,
            ids=id_list,
        )
        present: set[str] = set()
        async for record in result:
            present.add(record["id"])
        return present

    async def write_edges(
        self,
        *,
        attempt_id: int,
        edges: list[Edge],
        source: str,
    ) -> WriteEdgesResult:
        """Create typed edges, validating BEFORE issuing any CREATE.

        Each edge is classified in one pass: an edge whose endpoint is absent
        from the subgraph is DROPPED (`endpoint_absent`); an edge whose
        endpoint types are unknown or violate EDGE_ALLOWED_PAIRS is INVALID
        (`unknown_endpoint_type` / `disallowed_pair`). Both are LOGGED with a
        reason (`write_edge_rejected`) and never silently lost — the store-side
        mirror of the parser boundary's `parser_edge_rejected`. Surviving edges
        are grouped by type and CREATEd. Endpoint existence is checked in ONE
        scoped read shared with the writes. Returns a `WriteEdgesResult`
        (written/dropped/invalid + per-rejection reasons; `int()`-coercible to
        `written` for back-compat). Rejections are data, not exceptions.
        """
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)
        if not edges:
            return WriteEdgesResult()

        endpoint_ids = {e.from_node_id for e in edges} | {e.to_node_id for e in edges}

        written = 0
        dropped = 0
        invalid = 0
        reasons: list[tuple[str, str]] = []

        async with self.neo.session() as s:
            present = await self._existing_node_ids(s, attempt_id, endpoint_ids)

            rows_by_type: dict[str, list[dict[str, Any]]] = {}
            for e in edges:
                repr_ = _edge_repr(e)
                if e.from_node_id not in present or e.to_node_id not in present:
                    dropped += 1
                    reasons.append((repr_, "endpoint_absent"))
                    _LOG.info("write_edge_rejected reason=endpoint_absent edge=%s", repr_)
                    continue
                if e.from_node_type is None or e.to_node_type is None:
                    invalid += 1
                    reasons.append((repr_, "unknown_endpoint_type"))
                    _LOG.info("write_edge_rejected reason=unknown_endpoint_type edge=%s", repr_)
                    continue
                pair = (e.from_node_type, e.to_node_type)
                if pair not in EDGE_ALLOWED_PAIRS[e.edge_type]:
                    invalid += 1
                    reasons.append((repr_, "disallowed_pair"))
                    _LOG.info("write_edge_rejected reason=disallowed_pair edge=%s", repr_)
                    continue
                row = _edge_to_neo4j_row(e)
                row["attempt_id"] = attempt_id
                row["source"] = source
                rows_by_type.setdefault(e.edge_type.value, []).append(row)

            for et, rows in rows_by_type.items():
                cypher = _EDGE_CREATE_CYPHER[et]
                result = await s.run(cypher, rows=rows)
                rec = await result.single()
                written += int(rec["written"]) if rec else 0

        _LOG.info(
            "write_edges attempt_id=%s written=%s dropped=%s invalid=%s reasons=%r",
            attempt_id,
            written,
            dropped,
            invalid,
            tuple(reasons),
        )
        return WriteEdgesResult(
            written=written,
            dropped=dropped,
            invalid=invalid,
            reasons=tuple(reasons),
        )

    async def read_graph(self, *, attempt_id: int) -> KGGraph:
        """Read the full per-attempt subgraph."""
        async with self.neo.session() as s:
            nodes_res = await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid}) RETURN n AS props, labels(n) AS labels",
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
        in the per-attempt subgraph. Safe to call when nothing exists.

        WU-3C1 retention (§7): `handle_end` NO LONGER calls this — per-attempt
        subgraphs now PERSIST. This stays as the future janitor's pruning
        primitive AND `restart_problem`'s explicit student wipe.
        """
        async with self.neo.session() as s:
            await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid}) DETACH DELETE n",
                aid=attempt_id,
            )

    async def stamp_graded_at(self, *, attempt_id: int) -> int:
        """Stamp `graded_at` (ISO-8601 UTC) on every node of the frozen
        per-attempt subgraph at Done (§7 / §6.4). Returns the count stamped.

        Idempotent — re-running overwrites the same field, never duplicates.
        Δt-anchoring in Layer-3 (§3) reads the STORED value, never now(). Does
        NOT touch Postgres. Any Neo4j failure surfaces as a NO-FALLBACK
        `RetentionError` (the grade is already committed when this runs, so the
        failure is loud but never voids the grade; the next Done / retry /
        janitor re-stamps idempotently).
        """
        ts = _utc_now_iso()
        try:
            async with self.neo.session() as s:
                result = await s.run(
                    "MATCH (n:_KGNode {attempt_id: $aid}) "
                    "SET n.graded_at = $ts "
                    "RETURN count(n) AS stamped",
                    aid=attempt_id,
                    ts=ts,
                )
                rec = await result.single()
                return int(rec["stamped"]) if rec else 0
        except RetentionError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a named error
            raise RetentionError(attempt_id=attempt_id, last_error=str(exc)) from exc

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

    # ------- Negotiable OLM (P3) --------------------------------------------
    #
    # Three moves the student can make against any KG entry:
    #   challenge   — flag for grader review (status -> DISPUTED)
    #   paraphrase  — supply preferred surface form (status -> DUAL,
    #                 student_belief = surface_form). The structural fields
    #                 (symbolic, label, etc.) are preserved; only the
    #                 student-facing belief is recorded.
    #   skip        — pass through w/o edits (status -> DUAL with no belief).
    #                 The "I'm not going to fight about it" escape hatch.
    #
    # Each move:
    #   1. Verifies the session is unfrozen (Done freeze blocks late moves).
    #   2. Updates the Neo4j node in place.
    #   3. Logs an audit-trail row in apollo_kg_negotiations (Postgres).
    #   4. Returns the updated typed Node.
    #
    # The Done-gate (P3.6) reads `status` directly from the graph and the
    # negotiation log to decide whether the student has "touched" each
    # flagged entry.

    async def _set_node_status_neo4j(
        self,
        *,
        attempt_id: int,
        node_id: str,
        set_clause: str,
        params: dict[str, Any],
    ) -> Node:
        """Run a parameterized SET-and-RETURN Cypher and reconstruct the
        typed Node. Raises KGEntryNotFoundError when the node is absent.

        Internal helper — the three move methods build the same SET shape
        and read it back through here to avoid duplicating reconstruction
        logic three times.
        """
        cypher = (
            "MATCH (n:_KGNode {attempt_id: $aid, node_id: $nid}) "
            f"SET {set_clause} "
            "RETURN n AS props, labels(n) AS labels"
        )
        all_params = {"aid": attempt_id, "nid": node_id, **params}
        async with self.neo.session() as s:
            result = await s.run(cypher, **all_params)
            rec = await result.single()
        if rec is None:
            raise KGEntryNotFoundError(attempt_id=attempt_id, node_id=node_id)
        return _record_to_node(dict(rec["props"]), list(rec["labels"]))

    async def _log_negotiation(
        self,
        *,
        attempt_id: int,
        node_id: str,
        move: str,
        payload: dict[str, Any],
    ) -> None:
        """Append a row to apollo_kg_negotiations. The audit log is
        append-only — every move gets its own row even on the same entry."""
        self.db.add(
            KGNegotiation(
                attempt_id=attempt_id,
                entry_id=node_id,
                actor="student",
                move=move,
                payload=payload,
            )
        )
        await self.db.commit()

    async def mark_node_disputed(
        self,
        *,
        attempt_id: int,
        node_id: str,
        reason: str,
    ) -> Node:
        """CHALLENGE: status -> DISPUTED. Logs the student's reason. The
        node's structural content is preserved — only `status` changes."""
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)

        node = await self._set_node_status_neo4j(
            attempt_id=attempt_id,
            node_id=node_id,
            set_clause="n.status = 'DISPUTED'",
            params={},
        )
        await self._log_negotiation(
            attempt_id=attempt_id,
            node_id=node_id,
            move="challenge",
            payload={"reason": reason},
        )
        return node

    async def paraphrase_node(
        self,
        *,
        attempt_id: int,
        node_id: str,
        surface_form: str,
    ) -> Node:
        """SUPPLY-PARAPHRASE: status -> DUAL, student_belief = surface_form.

        Preserves all structural fields (symbolic, label, variables, etc.).
        Only the human-readable surface form is recorded as the student's
        wording. Coverage uses student_belief for grading DUAL entries (P3.4).
        """
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)

        node = await self._set_node_status_neo4j(
            attempt_id=attempt_id,
            node_id=node_id,
            set_clause="n.status = 'DUAL', n.student_belief = $belief",
            params={"belief": surface_form},
        )
        await self._log_negotiation(
            attempt_id=attempt_id,
            node_id=node_id,
            move="paraphrase",
            payload={"surface_form": surface_form},
        )
        return node

    async def skip_node(
        self,
        *,
        attempt_id: int,
        node_id: str,
    ) -> Node:
        """SKIP: status -> DUAL with no edits. Flags the entry for the
        grader without forcing the student to articulate a paraphrase."""
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)

        node = await self._set_node_status_neo4j(
            attempt_id=attempt_id,
            node_id=node_id,
            set_clause="n.status = 'DUAL'",
            params={},
        )
        await self._log_negotiation(
            attempt_id=attempt_id,
            node_id=node_id,
            move="skip",
            payload={},
        )
        return node

    async def get_node_trace(
        self,
        *,
        attempt_id: int,
        node_id: str,
    ) -> dict[str, Any]:
        """Ordered audit trail for one entry. Returns:

            {
              "node_id": str,
              "moves": [
                {"actor": str, "move": str, "payload": dict, "created_at": iso8601},
                ...
              ],
              "source_utterance": str | None,  # most recent student message
                                                # in the attempt — best-effort
            }

        The source_utterance is approximate (latest student message rather
        than "the message that produced this node") because Neo4j nodes
        do not currently carry a source_message_id. The common case the
        FE renders ("you just said X and Apollo misheard") works correctly
        because the student typically negotiates immediately after the
        offending turn.
        """
        # Audit trail — chronological.
        rows = (
            (
                await self.db.execute(
                    select(KGNegotiation)
                    .where(KGNegotiation.attempt_id == attempt_id)
                    .where(KGNegotiation.entry_id == node_id)
                    .order_by(KGNegotiation.created_at.asc(), KGNegotiation.id.asc())
                )
            )
            .scalars()
            .all()
        )

        moves: list[dict[str, Any]] = []
        for r in rows:
            moves.append(
                {
                    "actor": r.actor,
                    "move": r.move,
                    "payload": r.payload or {},
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

        # Source utterance — latest student message in the attempt.
        last_student_msg = (
            await self.db.execute(
                select(Message.content)
                .where(Message.attempt_id == attempt_id)
                .where(Message.role == "student")
                .order_by(Message.turn_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        return {
            "node_id": node_id,
            "moves": moves,
            "source_utterance": last_student_msg,
        }
