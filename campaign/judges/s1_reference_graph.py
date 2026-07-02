"""S1 — reference-graph audit (spec §4 table, row 1).

Item = one provisioned subject's minted reference graph (nodes+edges dumped
from Postgres entities + the subject's `problems/problem_*.json`). Checks:
nodes are real concept steps, PRECEDES/USES edges are true, nothing
missing/duplicated/cyclic. The cycle/duplicate check is CODE (deterministic —
mirrors ``KGGraph.topological_order``'s Kahn's-algorithm shape), not an LLM
call, per the plan ("cycle check is CODE ... not the LLM"); everything else
(is this node a real step, is this edge true) is the LLM's job because it
requires reading the source material.

Gate (E3): >=95% item-level correct.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

from campaign.judges.base import JudgeResult, StageJudge, Verdict, aggregate

__all__ = ["S1ReferenceGraphJudge", "find_structural_defects"]

_SYSTEM_PROMPT = (
    "You are auditing a minted knowledge-graph reference solution against its "
    "source problem. You see ONLY the subject's problem statement, the "
    "authored reference solution, and ONE node or edge from the minted graph "
    "— never the full pipeline. For a NODE: is it a real, correct step of "
    "the solution (not hallucinated, not a duplicate/paraphrase of another "
    "step, not off-topic)? For an EDGE: is the claimed PRECEDES/USES "
    "relationship actually true given the two endpoints? Answer strictly "
    "from the given material."
)


def find_structural_defects(
    nodes: list[Mapping[str, Any]], edges: list[Mapping[str, Any]]
) -> list[Verdict]:
    """Deterministic structural checks: duplicate node ids and PRECEDES
    cycles. Returns one :class:`Verdict` per defect found (empty if the graph
    is structurally sound). Cycle detection reuses Kahn's-algorithm (the same
    shape as ``KGGraph.topological_order``) restricted to PRECEDES edges."""
    verdicts: list[Verdict] = []

    seen: dict[str, int] = {}
    for node in nodes:
        node_id = str(node.get("node_id", ""))
        seen[node_id] = seen.get(node_id, 0) + 1
    for node_id, count in seen.items():
        if count > 1:
            verdicts.append(
                Verdict(
                    item_id=f"structure:duplicate:{node_id}",
                    ok=False,
                    reason=f"node_id {node_id!r} appears {count} times",
                )
            )

    node_ids = set(seen.keys())
    precedes = [e for e in edges if e.get("edge_type") == "PRECEDES"]
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    adj: dict[str, list[str]] = defaultdict(list)
    for edge in precedes:
        src, dst = str(edge.get("from_node_id")), str(edge.get("to_node_id"))
        if src not in node_ids or dst not in node_ids:
            continue
        adj[src].append(dst)
        in_degree[dst] += 1

    queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
    ordered = 0
    while queue:
        nid = queue.popleft()
        ordered += 1
        for nxt in adj[nid]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if node_ids and ordered != len(node_ids):
        verdicts.append(
            Verdict(
                item_id="structure:cycle",
                ok=False,
                reason=(
                    f"PRECEDES subgraph has a cycle: only {ordered}/{len(node_ids)} "
                    "nodes are topologically orderable"
                ),
            )
        )

    return verdicts


class S1ReferenceGraphJudge(StageJudge):
    stage = "s1_reference_graph"
    system_prompt = _SYSTEM_PROMPT

    def build_items(self, raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``raw`` = list of ``{subject, problem, nodes, edges}`` dicts (one
        per provisioned subject). Emits one item per node + one per edge,
        each carrying its own subject's problem statement for grounding, PLUS
        one code-only structural item per subject (never sent to the LLM —
        filtered out again by ``judge`` via ``kind == "structural"``)."""
        items: list[dict[str, Any]] = []
        for subject in raw:
            subject_key = subject.get("subject", "")
            problem = subject.get("problem", {})
            nodes = list(subject.get("nodes", []))
            edges = list(subject.get("edges", []))
            for node in nodes:
                items.append(
                    {
                        "kind": "node",
                        "item_id": f"{subject_key}:node:{node.get('node_id')}",
                        "subject": subject_key,
                        "problem": problem,
                        "entity": node,
                    }
                )
            for edge in edges:
                items.append(
                    {
                        "kind": "edge",
                        "item_id": (
                            f"{subject_key}:edge:{edge.get('edge_type')}:"
                            f"{edge.get('from_node_id')}->{edge.get('to_node_id')}"
                        ),
                        "subject": subject_key,
                        "problem": problem,
                        "entity": edge,
                    }
                )
            for defect in find_structural_defects(nodes, edges):
                items.append(
                    {
                        "kind": "structural",
                        "item_id": f"{subject_key}:{defect.item_id}",
                        "verdict": defect,
                    }
                )
        return items

    def user_prompt(self, item: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "kind": item["kind"],
                "problem": item["problem"],
                "entity": item["entity"],
            },
            sort_keys=True,
        )

    async def judge(self, raw: Any) -> JudgeResult:
        items = self.build_items(raw)
        verdicts: list[Verdict] = []
        schema = self.schema()
        for item in items:
            if item["kind"] == "structural":
                defect: Verdict = item["verdict"]
                verdicts.append(
                    Verdict(item_id=item["item_id"], ok=defect.ok, reason=defect.reason)
                )
                continue
            response = await self._llm.judge_item(
                system_prompt=self.system_prompt,
                user_prompt=self.user_prompt(item),
                schema=schema,
            )
            verdicts.append(
                Verdict(
                    item_id=item["item_id"],
                    ok=bool(response.get("ok", False)),
                    reason=str(response.get("reason", "")),
                )
            )
        passed, total, pass_rate = aggregate(verdicts)
        return JudgeResult(
            stage=self.stage,
            verdicts=tuple(verdicts),
            passed=passed,
            total=total,
            pass_rate=pass_rate,
        )
