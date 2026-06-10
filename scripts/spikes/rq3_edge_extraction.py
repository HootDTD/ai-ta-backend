"""RQ3 spike: can GPT-4o reliably emit valid typed KG edges in ONE call when
given (a) the EDGE_ALLOWED_PAIRS vocabulary and (b) the existing attempt graph
as context, under strict structured outputs?

Research plan: docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-research-plan.md (RQ3).
Replays multi-turn teaching transcripts turn-by-turn, accumulating graph
context across turns, and measures:
  - edge validity rate (allowed pair + resolvable endpoints + no self-loops)
  - cross-turn edge count (links into nodes from earlier turns)
  - duplicate-avoidance events (model flags reuse of an existing node)
  - orphan rate after each transcript vs the current-parser baseline
    (baseline = only within-turn USES + PRECEDES, today's deterministic rules)
  - latency + token cost per call

NOTE: prod transcript replay was blocked by DB access policy and prod held
only one attempt at investigation time; transcripts below are synthetic but
grounded in the authored problem bank (problems/problem_01.json) and the
documented real utterance style ("Use bernulis equation ... use continuity",
2026-06-09 handoff). Run from ai-ta-backend/:
  .venv/Scripts/python.exe scripts/spikes/rq3_edge_extraction.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from openai import OpenAI

from apollo.ontology.edges import EDGE_ALLOWED_PAIRS, EdgeType

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

MODEL = os.getenv("MAIN_MODEL", "gpt-4o")

# ---------------------------------------------------------------- prompt ----

EDGE_VOCAB = """\
Edge vocabulary (the ONLY allowed edge types and endpoint-type pairs):
- PRECEDES: procedure_step -> procedure_step (this step comes before that step)
- USES: procedure_step -> equation (the step applies that equation)
- SCOPES: condition -> equation, or simplification -> equation
  (the condition/simplification governs when/how that equation applies)
- DEPENDS_ON: any -> any except self-loops (one entry relies on another,
  e.g. an equation depends on a definition, a step depends on a condition)
Never invent other edge types or endpoint pairs."""

SYSTEM_PROMPT = f"""You extract structured knowledge-graph entries AND typed edges
from a student's explanation of a fluid mechanics (Bernoulli's principle) concept.

For each entry, type-specific content fields (put unused fields to null):
- equation: "symbolic" (parseable, zero-form LHS - (RHS), ** for powers,
  Rational(1,2) for halves), "label" (what the student called it),
  "variables" (list of symbols).
- condition: "applies_when", "label".
- simplification: "applies_when", "transformation".
- definition: "concept", "meaning".
- variable_mapping: "term", "symbol".
- procedure_step: "action", "purpose". Extract ONLY plan-speak (what the
  student/solver would DO), not physical causation.

{EDGE_VOCAB}

You also receive EXISTING GRAPH: entries extracted from the student's EARLIER
messages in this session, each with a stable id, its type, and a short label.
Rules:
1. Extract ONLY what the student said in the CURRENT message. Do not add physics
   they did not mention. Do not correct them; extract errors as stated.
2. If the current message refers to something already in EXISTING GRAPH (by name,
   typo'd name, paraphrase, or "that equation"), DO NOT create a duplicate entry.
   Reference the existing id in edges, and if the current message adds no new
   content for it, set "reuse_of" to that existing id on a stub entry ONLY when
   you need it for clarity — prefer just using the existing id in edges.
3. Emit every edge that the student's wording justifies, including edges whose
   endpoints are EXISTING GRAPH ids (cross-turn links). Edge endpoint refs:
   "n<i>" = the i-th entry of THIS response (0-based), or an existing graph id.
4. Confidence per entry in [0,1]: 1.0 explicit/verbatim, 0.8 clearly inferable,
   0.6 paraphrased, 0.4 ambiguous guess, 0.2 very uncertain.
5. If nothing is extractable, return empty lists."""

RESPONSE_SCHEMA = {
    "name": "kg_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entries", "edges"],
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "confidence", "reuse_of", "symbolic",
                                 "label", "variables", "applies_when",
                                 "transformation", "concept", "meaning",
                                 "term", "symbol", "action", "purpose"],
                    "properties": {
                        "type": {"type": "string", "enum": [
                            "equation", "condition", "simplification",
                            "definition", "variable_mapping", "procedure_step"]},
                        "confidence": {"type": "number"},
                        "reuse_of": {"type": ["string", "null"]},
                        "symbolic": {"type": ["string", "null"]},
                        "label": {"type": ["string", "null"]},
                        "variables": {"type": ["array", "null"],
                                      "items": {"type": "string"}},
                        "applies_when": {"type": ["string", "null"]},
                        "transformation": {"type": ["string", "null"]},
                        "concept": {"type": ["string", "null"]},
                        "meaning": {"type": ["string", "null"]},
                        "term": {"type": ["string", "null"]},
                        "symbol": {"type": ["string", "null"]},
                        "action": {"type": ["string", "null"]},
                        "purpose": {"type": ["string", "null"]},
                    },
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["edge_type", "from_ref", "to_ref"],
                    "properties": {
                        "edge_type": {"type": "string", "enum": [
                            "PRECEDES", "USES", "DEPENDS_ON", "SCOPES"]},
                        "from_ref": {"type": "string"},
                        "to_ref": {"type": "string"},
                    },
                },
            },
        },
    },
}

# ----------------------------------------------------------- transcripts ----
# Grounded in problems/problem_01.json (horizontal pipe, find P2) and the
# documented real student style (terse, typos: "bernulis").

TRANSCRIPTS: dict[str, list[str]] = {
    "T1_faithful_multiturn": [
        "First we need the continuity equation, rho*A1*v1 = rho*A2*v2. It works because mass is conserved.",
        "Bernoulli's equation is P1 + 1/2*rho*v1^2 + rho*g*h1 = P2 + 1/2*rho*v2^2 + rho*g*h2. It only applies when the density is constant.",
        "Since the pipe is horizontal h1 equals h2 so the rho*g*h terms cancel from Bernoulli.",
        "So first I'd use continuity to get v2, then I'd plug v2 into the simplified Bernoulli and solve for P2.",
    ],
    "T2_typos_terse": [
        "Use bernulis equation to find the presure. also use continuity",
        "bernulis is P1 + 0.5*rho*v1^2 = P2 + 0.5*rho*v2^2 here since its flat",
        "first solve continuity for v2 then put it in bernulis and get P2",
    ],
    "T3_late_conditions": [
        "The key equation is Bernoulli: P + 1/2 rho v^2 + rho g h is constant along a streamline.",
        "P stands for pressure, rho is the density of the fluid, and v is the flow speed.",
        "Oh I should say — that Bernoulli equation only works if the flow is steady and the fluid is incompressible.",
        "And because this pipe is horizontal, the height term drops out of that equation.",
    ],
    "T4_definitions_first": [
        "Pressure is the force per unit area the fluid pushes with. Velocity is how fast the fluid moves.",
        "Where the pipe gets narrower the fluid has to speed up, that's continuity: A1*v1 = A2*v2.",
        "Step one: use that continuity equation I just gave to find v2. Step two: use Bernoulli P1 + 1/2*rho*v1^2 = P2 + 1/2*rho*v2^2 to find P2.",
    ],
    "T5_duplicates_and_filler": [
        "Bernoulli's equation: P1 + 1/2*rho*v1^2 + rho*g*h1 = P2 + 1/2*rho*v2^2 + rho*g*h2",
        "wait do you know what continuity is?",
        "Continuity means A1*v1 = A2*v2 for an incompressible fluid.",
        "So to recap, we have Bernoulli's equation P1 + 1/2*rho*v1^2 + rho*g*h1 = P2 + 1/2*rho*v2^2 + rho*g*h2, and continuity A1*v1 = A2*v2. Use continuity first for v2, then Bernoulli for P2.",
    ],
}

# ------------------------------------------------------------- replay ------


def _entry_summary(entry: dict, node_id: str) -> str:
    label = (entry.get("label") or entry.get("concept") or entry.get("term")
             or entry.get("action") or entry.get("applies_when") or "")
    return f"{node_id} [{entry['type']}] {label[:60]}"


def replay_transcript(client: OpenAI, name: str, turns: list[str]) -> dict:
    graph_nodes: dict[str, str] = {}  # node_id -> node_type
    graph_lines: list[str] = []
    node_degree: dict[str, int] = {}
    baseline_degree: dict[str, int] = {}  # only within-turn USES/PRECEDES
    stats = {
        "transcript": name, "turns": len(turns), "calls": [],
        "edges_total": 0, "edges_valid": 0, "edges_cross_turn": 0,
        "edge_errors": [], "reuse_events": 0, "entries_total": 0,
    }

    for ti, utterance in enumerate(turns):
        context = ("EXISTING GRAPH:\n" + "\n".join(graph_lines)
                   if graph_lines else "EXISTING GRAPH: (empty)")
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=MODEL,
            response_format={"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{context}\n\nCURRENT MESSAGE:\n{utterance}"},
            ],
            temperature=0.0,
        )
        latency = time.perf_counter() - t0
        payload = json.loads(resp.choices[0].message.content or "{}")
        usage = resp.usage
        stats["calls"].append({
            "turn": ti, "latency_s": round(latency, 2),
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "entries": len(payload.get("entries", [])),
            "edges": len(payload.get("edges", [])),
        })

        # Register this turn's entries.
        turn_ids: dict[str, str] = {}  # "n0" -> node_id
        for i, entry in enumerate(payload.get("entries", [])):
            stats["entries_total"] += 1
            if entry.get("reuse_of"):
                stats["reuse_events"] += 1
                if entry["reuse_of"] in graph_nodes:
                    turn_ids[f"n{i}"] = entry["reuse_of"]
                    continue
            node_id = f"t{ti}_n{i}"
            turn_ids[f"n{i}"] = node_id
            graph_nodes[node_id] = entry["type"]
            node_degree.setdefault(node_id, 0)
            baseline_degree.setdefault(node_id, 0)
            graph_lines.append(_entry_summary(entry, node_id))

        # Validate edges.
        for edge in payload.get("edges", []):
            stats["edges_total"] += 1
            refs = []
            for ref in (edge["from_ref"], edge["to_ref"]):
                refs.append(turn_ids.get(ref, ref))
            frm, to = refs
            if frm not in graph_nodes or to not in graph_nodes:
                stats["edge_errors"].append(
                    f"turn {ti}: unresolvable endpoint {edge}")
                continue
            if frm == to:
                stats["edge_errors"].append(f"turn {ti}: self-loop {edge}")
                continue
            pair = (graph_nodes[frm], graph_nodes[to])
            try:
                et = EdgeType(edge["edge_type"])
            except ValueError:
                stats["edge_errors"].append(f"turn {ti}: bad type {edge}")
                continue
            if pair not in EDGE_ALLOWED_PAIRS[et]:
                stats["edge_errors"].append(
                    f"turn {ti}: disallowed pair {et}:{pair} {edge}")
                continue
            stats["edges_valid"] += 1
            node_degree[frm] += 1
            node_degree[to] += 1
            cross_turn = (not frm.startswith(f"t{ti}_")
                          or not to.startswith(f"t{ti}_"))
            if cross_turn:
                stats["edges_cross_turn"] += 1
            # Baseline simulation: today's parser only creates within-turn
            # USES (proc->eq) and PRECEDES (proc->proc) edges.
            if not cross_turn and et in (EdgeType.USES, EdgeType.PRECEDES):
                baseline_degree[frm] += 1
                baseline_degree[to] += 1

    n = len(graph_nodes)
    stats["nodes_final"] = n
    stats["orphans_prototype"] = sum(1 for d in node_degree.values() if d == 0)
    stats["orphans_baseline"] = sum(1 for d in baseline_degree.values() if d == 0)
    return stats


def main() -> None:
    client = OpenAI()
    results = [replay_transcript(client, name, turns)
               for name, turns in TRANSCRIPTS.items()]

    out = Path(__file__).with_name("rq3_results.json")
    out.write_text(json.dumps(results, indent=2))

    print(f"\nmodel={MODEL}  transcripts={len(results)}")
    print(f"{'transcript':28} {'nodes':>5} {'edges':>5} {'valid':>5} "
          f"{'xturn':>5} {'orph(new)':>9} {'orph(base)':>10} {'reuse':>5}")
    for r in results:
        print(f"{r['transcript']:28} {r['nodes_final']:>5} {r['edges_total']:>5} "
              f"{r['edges_valid']:>5} {r['edges_cross_turn']:>5} "
              f"{r['orphans_prototype']:>9} {r['orphans_baseline']:>10} "
              f"{r['reuse_events']:>5}")
    all_calls = [c for r in results for c in r["calls"]]
    lat = [c["latency_s"] for c in all_calls]
    pt = sum(c["prompt_tokens"] for c in all_calls)
    ct = sum(c["completion_tokens"] for c in all_calls)
    ev = sum(r["edges_valid"] for r in results)
    et = sum(r["edges_total"] for r in results)
    print(f"\ncalls={len(all_calls)}  latency median={statistics.median(lat):.2f}s "
          f"p max={max(lat):.2f}s")
    print(f"tokens prompt={pt} completion={ct} "
          f"(~${(pt * 2.5 + ct * 10) / 1e6:.3f} at gpt-4o pricing)")
    print(f"edge validity: {ev}/{et} = {ev / et:.0%}" if et else "no edges")
    errs = [e for r in results for e in r["edge_errors"]]
    if errs:
        print("\nedge errors:")
        for e in errs:
            print("  -", e)
    print(f"\nresults written to {out}")


if __name__ == "__main__":
    main()
