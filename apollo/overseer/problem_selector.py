"""Problem selection backed by Neo4j. Returns the same Problem Pydantic object
Apollo handlers already consume. Hand-authored problems (authored=true) sort
before extracted problems regardless of id collation."""
from __future__ import annotations

import json
from typing import List, Sequence

from apollo.errors import PoolExhaustedError
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import Problem, ReferenceStep

_ENTRY_TYPE_BY_NODE_TYPE = {
    "equation": "equation", "condition": "condition",
    "simplification": "simplification", "definition": "definition",
    "variable_mapping": "variable_mapping", "procedure_step": "procedure_step",
}


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
    return [await _load_problem(r["id"], neo) for r in rows]


async def _load_problem(problem_id: str, neo: Neo4jClient) -> Problem:
    async with neo.session() as s:
        prec = await (await s.run(
            "MATCH (p:Problem {problem_id:$p}) RETURN p", p=problem_id)).single()
        node_rows = await (await s.run(
            "MATCH (p:Problem {problem_id:$p})-[:HAS_REFERENCE_NODE]->(n:_ProblemNode) "
            "RETURN n.node_id AS id, n.node_type AS type, n.content AS content", p=problem_id)).data()
        dep_rows = await (await s.run(
            "MATCH (a:_ProblemNode {problem_id:$p})-[:DEPENDS_ON]->(b:_ProblemNode {problem_id:$p}) "
            "RETURN a.node_id AS frm, b.node_id AS to", p=problem_id)).data()
    p = prec["p"]
    deps: dict[str, list[str]] = {}
    for d in dep_rows:
        deps.setdefault(d["frm"], []).append(d["to"])
    steps = []
    for i, n in enumerate(sorted(node_rows, key=lambda r: r["id"]), start=1):
        steps.append(ReferenceStep(
            step=i, entry_type=_ENTRY_TYPE_BY_NODE_TYPE[n["type"]], id=n["id"],
            content=json.loads(n["content"]), depends_on=deps.get(n["id"], [])))
    return Problem(
        id=p["problem_id"], concept_id=p["concept_id"], difficulty=p["difficulty"],
        problem_text=p["problem_text"], given_values=json.loads(p["given_values"]),
        target_unknown=p["target_unknown"], reference_solution=steps)
