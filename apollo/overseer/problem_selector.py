"""Problem selection backed by Neo4j. Returns the same Problem Pydantic object
Apollo handlers already consume. Hand-authored problems (authored=true) sort
before extracted problems regardless of id collation."""
from __future__ import annotations

import json
from typing import List, Sequence

from apollo.errors import PoolExhaustedError
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import Problem, ReferenceStep


async def cluster_to_concept(cluster_id: str, neo: Neo4jClient) -> tuple[str, str]:
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (a:ClusterAlias {cluster_id:$c})-[:RESOLVES_TO]->(concept:Concept) "
            "RETURN concept.subject_id AS s, concept.concept_id AS cid",
            c=cluster_id)).single()
    if rec is None:
        raise KeyError(f"no ClusterAlias for cluster_id {cluster_id!r}")
    return rec["s"], rec["cid"]


async def select_problem(*, cluster_id: str, difficulty: str,
                         attempted_ids: Sequence[str], neo: Neo4jClient) -> Problem:
    subject_id, concept_id = await cluster_to_concept(cluster_id, neo)
    async with neo.session() as s:
        rec = await (await s.run(
            """
            MATCH (p:Problem {subject_id:$s, concept_id:$c, difficulty:$d})
            WHERE NOT p.problem_id IN $attempted
            RETURN p ORDER BY p.authored DESC, p.problem_id ASC LIMIT 1
            """,
            s=subject_id, c=concept_id, d=difficulty,
            attempted=list(attempted_ids))).single()
        if rec is None:
            raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
        return await _load_problem(rec["p"]["problem_id"], neo)


async def list_problems_for_cluster(cluster_id: str, neo: Neo4jClient) -> List[Problem]:
    try:
        subject_id, concept_id = await cluster_to_concept(cluster_id, neo)
    except KeyError:
        return []
    async with neo.session() as s:
        rows = await (await s.run(
            "MATCH (p:Problem {subject_id:$s, concept_id:$c}) "
            "RETURN p.problem_id AS id ORDER BY p.authored DESC, p.problem_id ASC",
            s=subject_id, c=concept_id)).data()
    # Full problems are loaded (not lightweight stubs) because callers like done.py need the
    # full reference_solution for grading, and problem banks are small; batching is a future optimization.
    return [await _load_problem(r["id"], neo) for r in rows]


async def _load_problem(problem_id: str, neo: Neo4jClient) -> Problem:
    async with neo.session() as s:
        prec = await (await s.run(
            "MATCH (p:Problem {problem_id:$p}) RETURN p", p=problem_id)).single()
        node_rows = await (await s.run(
            "MATCH (p:Problem {problem_id:$p})-[:HAS_REFERENCE_NODE]->(n:_ProblemNode) "
            "RETURN n.node_id AS id, n.node_type AS type, n.step_num AS step_num, n.content AS content", p=problem_id)).data()
        dep_rows = await (await s.run(
            "MATCH (a:_ProblemNode {problem_id:$p})-[:DEPENDS_ON]->(b:_ProblemNode {problem_id:$p}) "
            "RETURN a.node_id AS frm, b.node_id AS to", p=problem_id)).data()
        # PRECEDES encodes procedure_step order; USES encodes uses_equations.
        # Both are stripped from content on write and must be reconstructed here.
        precedes_rows = await (await s.run(
            "MATCH (a:_ProblemNode {problem_id:$p})-[:PRECEDES]->(b:_ProblemNode {problem_id:$p}) "
            "RETURN a.node_id AS frm, b.node_id AS to", p=problem_id)).data()
        uses_rows = await (await s.run(
            "MATCH (a:_ProblemNode {problem_id:$p})-[:USES]->(b:_ProblemNode {problem_id:$p}) "
            "RETURN a.node_id AS frm, b.node_id AS to", p=problem_id)).data()
    p = prec["p"]
    deps: dict[str, list[str]] = {}
    for d in dep_rows:
        deps.setdefault(d["frm"], []).append(d["to"])

    # Reconstruct procedure_step order from PRECEDES chain (topological sort).
    # Build adjacency: frm -> to (next step), then walk chains to assign order=1..N.
    precedes_next: dict[str, str] = {r["frm"]: r["to"] for r in precedes_rows}
    precedes_prev: set[str] = {r["to"] for r in precedes_rows}
    proc_order: dict[str, int] = {}
    # Find all procedure_step node ids
    proc_ids = {n["id"] for n in node_rows if n["type"] == "procedure_step"}
    # Chain heads are proc_ids not pointed to by any PRECEDES edge
    heads = proc_ids - precedes_prev
    for head in sorted(heads):  # sort for determinism when multiple chains exist
        order = 1
        cur: str | None = head
        while cur is not None:
            proc_order[cur] = order
            order += 1
            cur = precedes_next.get(cur)

    # Reconstruct uses_equations from USES edges
    uses_map: dict[str, list[str]] = {}
    for r in uses_rows:
        uses_map.setdefault(r["frm"], []).append(r["to"])

    steps = []
    for n in sorted(node_rows, key=lambda r: r["step_num"]):
        content = json.loads(n["content"])
        if n["type"] == "procedure_step":
            content["order"] = proc_order.get(n["id"], 1)
            content["uses_equations"] = sorted(uses_map.get(n["id"], []))
        steps.append(ReferenceStep(
            step=n["step_num"], entry_type=n["type"], id=n["id"],
            content=content, depends_on=deps.get(n["id"], [])))
    return Problem(
        id=p["problem_id"], concept_id=p["concept_id"], difficulty=p["difficulty"],
        problem_text=p["problem_text"], given_values=json.loads(p["given_values"]),
        target_unknown=p["target_unknown"], reference_solution=steps)
