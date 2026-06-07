# apollo/textbook_ingest/writer.py
"""Neo4j write primitives for concepts and problems. Stage-6 orchestration
(run_stage6) is added in a later task. First-writer-wins on concept policy is
enforced by skipping policy-child writes when the :Concept already exists.
"""
from __future__ import annotations

import json

from apollo.ontology.nodes import NODE_LABELS
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.textbook_ingest.concept_schema_map import entry_to_rows
from apollo.textbook_ingest.kg_convert import reference_steps_to_kg_graph
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem


async def concept_exists(neo: Neo4jClient, subject_id: str, concept_id: str) -> bool:
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$c}) RETURN count(c) AS n",
            s=subject_id, c=concept_id)).single()
        return rec["n"] > 0


async def problem_exists(neo: Neo4jClient, problem_id: str) -> bool:
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (p:Problem {problem_id:$p}) RETURN count(p) AS n",
            p=problem_id)).single()
        return rec["n"] > 0


async def write_concept(neo: Neo4jClient, entry: ConceptRegistryEntry, *,
                        source_document_id: str, scope_embedding: list[float],
                        policy_frozen: bool) -> None:
    """Create the :Concept and its policy children. Idempotent + first-writer-wins:
    if the concept already exists its policy children are left untouched."""
    if await concept_exists(neo, entry.subject_id, entry.concept_id):
        # check-then-act: safe under single-writer ingest; the concept_id_unique
        # constraint is the backstop if two writers ever race.
        return
    rows = entry_to_rows(entry)
    async with neo.session() as s:
        await s.execute_write(_write_concept_tx, rows, source_document_id,
                              scope_embedding, policy_frozen)


async def _write_concept_tx(tx, rows, source_document_id, scope_embedding, policy_frozen):
    c = rows["concept"]
    await tx.run(
        """
        CREATE (concept:Concept:_ConceptNode {
            subject_id:$subject_id, concept_id:$concept_id,
            scope_summary:$scope_summary, scope_embedding:$emb,
            parser_prompt_template:$ppt,
            non_trivial_keywords:$ntk, plan_markers:$pm,
            created_at:datetime(), source_document_id:$src, policy_frozen:$frozen })
        """,
        subject_id=c["subject_id"], concept_id=c["concept_id"],
        scope_summary=c["scope_summary"], emb=scope_embedding,
        ppt=c["parser_prompt_template"], ntk=c["non_trivial_keywords"],
        pm=c["plan_markers"], src=source_document_id, frozen=policy_frozen)
    for r in rows["symbols"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:HAS_SYMBOL]->(:CanonicalSymbol:_ConceptNode "
            "{concept_id:$cid, symbol:$symbol, description:$description, "
            "subscript_convention:$subscript_convention})",
            s=c["subject_id"], cid=c["concept_id"], **r)
    for r in rows["normalization"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:HAS_NORMALIZATION]->(:NormalizationEntry:_ConceptNode "
            "{concept_id:$cid, natural_language:$natural_language, "
            "canonical_symbol:$canonical_symbol})",
            s=c["subject_id"], cid=c["concept_id"], **r)
    for r in rows["solver_constants"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:HAS_CONSTANT]->(:SolverConstant:_ConceptNode "
            "{concept_id:$cid, name:$name, value:$value, kind:$kind})",
            s=c["subject_id"], cid=c["concept_id"], **r)
    for r in rows["forbidden_terms"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:FORBIDS]->(:ForbiddenTerm:_ConceptNode "
            "{concept_id:$cid, term:$term, category:$category})",
            s=c["subject_id"], cid=c["concept_id"], **r)


async def write_cluster_alias(neo: Neo4jClient, cluster_id: str,
                              subject_id: str, concept_id: str) -> None:
    async with neo.session() as s:
        created = await s.execute_write(_write_cluster_alias_tx,
                                        cluster_id, subject_id, concept_id)
    if not created:
        raise ValueError(
            f"cannot create cluster alias {cluster_id!r}: "
            f"concept {subject_id}/{concept_id} does not exist")


async def _write_cluster_alias_tx(tx, cluster_id, subject_id, concept_id) -> bool:
    rec = await (await tx.run(
        "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
        "MERGE (a:ClusterAlias {cluster_id:$cl}) "
        "MERGE (a)-[:RESOLVES_TO]->(c) "
        "RETURN count(c) AS n",
        s=subject_id, cid=concept_id, cl=cluster_id)).single()
    return rec is not None and rec["n"] > 0


async def write_problem(neo: Neo4jClient, problem: ValidatedProblem, *,
                        authored: bool) -> None:
    """Idempotent: skips if problem_id already exists."""
    if await problem_exists(neo, problem.problem_id):
        # check-then-act: safe under single-writer ingest; the problem_id_unique
        # constraint is the backstop if two writers ever race.
        return
    graph = reference_steps_to_kg_graph(problem.reference_solution)
    async with neo.session() as s:
        await s.execute_write(_write_problem_tx, problem, graph, authored)


async def _write_problem_tx(tx, problem, graph, authored):
    await tx.run(
        """
        CREATE (p:Problem:_ProblemNode {
            problem_id:$pid, subject_id:$sid, concept_id:$cid,
            difficulty:$diff, problem_text:$text, given_values:$givens,
            target_unknown:$target, source_document_id:$doc, source_page:$page,
            source_chunk_id:$chunk, authored:$authored, extracted_at:datetime() })
        """,
        pid=problem.problem_id, sid=problem.subject_id, cid=problem.concept_id,
        diff=problem.difficulty, text=problem.problem_text,
        givens=json.dumps(problem.given_values), target=problem.target_unknown,
        doc=problem.source_document_id, page=problem.source_page,
        chunk=problem.source_chunk_id, authored=authored)
    step_by_id = {rs.id: rs.step for rs in problem.reference_solution}
    for node in graph.nodes:
        label = NODE_LABELS[node.node_type]
        await tx.run(
            f"MATCH (p:Problem {{problem_id:$pid}}) "
            f"CREATE (p)-[:HAS_REFERENCE_NODE]->(n:{label}:_ProblemNode "
            f"{{problem_id:$pid, node_id:$nid, node_type:$ntype, step_num:$step, content:$content}})",
            pid=problem.problem_id, nid=node.node_id, ntype=node.node_type,
            step=step_by_id[node.node_id], content=node.content.model_dump_json())
    for edge in graph.edges:
        await tx.run(
            f"MATCH (a:_ProblemNode {{problem_id:$pid, node_id:$from}}), "
            f"(b:_ProblemNode {{problem_id:$pid, node_id:$to}}) "
            f"CREATE (a)-[:{edge.edge_type.value} {{problem_id:$pid}}]->(b)",
            pid=problem.problem_id, **{"from": edge.from_node_id, "to": edge.to_node_id})
