#!/usr/bin/env python3
"""Layer-2 end-to-end false-credit gate for a candidate NLI threshold config.

Unlike the pair-level sweep, this drives the FULL production resolver
(resolve_attempt -> polarity screen -> content-overlap floor -> semantic
shortlist -> NLI certify -> ambiguity margin -> misconception veto ->
competition) with the chosen (min_entailment, max_contradiction,
misconception_veto_entailment) for one model, over:

  - every authored POSITIVE premise (as a student node of the node's real type)
    against its full problem candidate set: must resolve to the INTENDED key or
    stay unresolved, but NEVER to a different reference key (= false credit).
  - every misconception-voicing premise (definition-type student node): must NOT
    resolve to any reference (veto/abstain), never credited.
  - the RESULTS-§2 smoke pairs, classified directly, as a model-sanity check.

A config is eligible for default-ON only if FALSE CREDITS == 0 here.

Usage (from ai-ta-backend/):
  .venv\\Scripts\\python.exe scripts\\nli_tuning_gate.py MODEL MIN_ENT MAX_CON VETO
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
SUBJECTS = ROOT / "apollo" / "subjects"

from nli_tuning_build_dev_set import GROUPS, PREMISES, VETO  # noqa: E402

from apollo.ontology.graph import KGGraph  # noqa: E402
from apollo.ontology.nodes import NodeType, build_node  # noqa: E402
from apollo.resolution.candidates import (  # noqa: E402
    build_candidate_set,
    candidates_from_misconceptions,
    candidates_from_reference_solution,
)
from apollo.resolution.embedding import CandidateEmbeddingCache  # noqa: E402
from apollo.resolution.nli_adjudicator import TransformersNLIAdjudicator  # noqa: E402
from apollo.resolution.nli_config import NLI_DEVICE, NLI_MODEL_NAME, NLIParams  # noqa: E402
from apollo.resolution.nli_resolution import NLIContext  # noqa: E402
from apollo.resolution.resolver import resolve_attempt  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else NLI_MODEL_NAME
MIN_ENT = float(sys.argv[2]) if len(sys.argv) > 2 else 0.70
MAX_CON = float(sys.argv[3]) if len(sys.argv) > 3 else 0.10
VETO_T = (
    float(sys.argv[4]) if len(sys.argv) > 4 else 0.96
)  # NOT `VETO` — that name is the imported dict
SLUG = MODEL.split("/")[-1]

CONCEPT_MISC = {
    "bern": "fluid_mechanics/concepts/bernoulli_principle/misconceptions.json",
    "gdp": "macroeconomics/concepts/gdp_components/misconceptions.json",
    "nvr": "macroeconomics/concepts/nominal_vs_real_gdp/misconceptions.json",
}
CONCEPT_VETO = {"bern": "bernoulli", "gdp": "gdp", "nvr": "nvr"}


def _load(p: Path) -> dict:
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _content_for(etype: str, premise: str) -> dict:
    if etype == "condition":
        return {"applies_when": premise}
    if etype == "simplification":
        # transformation must be non-empty (pydantic min_length=1); a single space
        # is stripped by student_surface_text so the surface == premise exactly.
        return {"applies_when": premise, "transformation": " "}
    if etype == "procedure_step":
        return {"action": premise}
    if etype == "definition":
        # concept must be non-empty; a single space is stripped so surface == premise.
        return {"concept": " ", "meaning": premise}
    raise ValueError(etype)


def _node(etype: str, nid: str, premise: str):
    return build_node(
        node_type=cast(NodeType, etype),
        node_id=nid,
        attempt_id=1,
        source="parser",
        content=_content_for(etype, premise),
    )


def main() -> None:
    print(f"[{SLUG}] gate: min_ent={MIN_ENT} max_con={MAX_CON} veto={VETO_T}", flush=True)
    adj = TransformersNLIAdjudicator(MODEL, device=NLI_DEVICE)
    params = NLIParams(
        min_entailment=MIN_ENT, max_contradiction=MAX_CON, misconception_veto_entailment=VETO_T
    )
    ctx = NLIContext(nli=adj, embedder=None, cache=CandidateEmbeddingCache(), params=params)

    false_credits: list[str] = []
    recovered = 0
    positives = 0
    veto_ok = 0
    veto_total = 0
    misc_false_credit = 0

    for gid, (cpath, pfile, keys) in GROUPS.items():
        concept_key = gid.split("_")[0]
        problem = _load(SUBJECTS / cpath / "problems" / pfile)
        misc = _load(SUBJECTS / CONCEPT_MISC[concept_key])
        refs = candidates_from_reference_solution(problem, canon_key_by_canonical_key={})
        miscs = candidates_from_misconceptions(misc, canon_key_by_canonical_key={})
        candidates = build_candidate_set(reference_nodes=refs, misconception_entities=miscs)
        etype_by_key = {
            s["entity_key"]: s["entry_type"] for s in problem.get("reference_solution", [])
        }

        # Build student graph: positives (right type) + misconception premises (definition).
        nodes = []
        meta: dict[str, dict] = {}
        for key in keys:
            etype = etype_by_key[key]
            for i, prem in enumerate(PREMISES.get(key, [])):
                nid = f"{gid}_{key}_{i}"
                nodes.append(_node(etype, nid, prem))
                meta[nid] = {"intended": key, "is_misc": False}
        # misconception nodes (one per veto_positive premise) — concept-scoped, added once per group
        for code, spec in VETO_DATA_FOR(concept_key):
            for j, prem in enumerate(spec["veto_positive"]):
                nid = f"{gid}_{code}_{j}"
                nodes.append(_node("definition", nid, prem))
                meta[nid] = {"intended": code, "is_misc": True}

        graph = KGGraph(nodes=nodes, edges=[])
        result = resolve_attempt(graph, candidates, nli_ctx=ctx)
        idx = {rn.node_id: rn for rn in result.resolved}

        for nid, m in meta.items():
            rn = idx[nid]
            resolved_key = rn.resolved_key if rn.resolution == "resolved" else None
            if m["is_misc"]:
                veto_total += 1
                # a misconception must NOT be credited to a reference (non-misc) key
                ref_keys = {c.canonical_key for c in candidates if not c.is_misconception}
                if resolved_key in ref_keys:
                    misc_false_credit += 1
                    false_credits.append(
                        f"MISC->REF {nid} ({m['intended']}) resolved to ref {resolved_key} via {rn.method}"
                    )
                else:
                    veto_ok += 1
            else:
                positives += 1
                if resolved_key is not None and resolved_key != m["intended"]:
                    false_credits.append(
                        f"WRONG-REF {nid} intended {m['intended']} resolved {resolved_key} via {rn.method}"
                    )
                elif resolved_key == m["intended"] and rn.method == "nli":
                    recovered += 1

    print("\n=== SMOKE (RESULTS §2) ===")
    for prem, hyp, expect in SMOKE:
        r = adj.classify(premise=prem, hypothesis=hyp)
        print(f"   [{expect:13}] ent={r.entailment:.3f} con={r.contradiction:.3f} lbl={r.label}")
        print(f"       '{prem[:55]}' -> '{hyp[:45]}'")

    print("\n" + "=" * 70)
    print(f"MODEL {SLUG}  min_ent={MIN_ENT} max_con={MAX_CON} veto={VETO_T}")
    print(f"positives={positives}  recovered_via_nli={recovered}")
    print(
        f"veto cases: {veto_ok}/{veto_total} correctly not-credited (misc->ref false credits={misc_false_credit})"
    )
    print(f"TOTAL FALSE CREDITS: {len(false_credits)}")
    for fc in false_credits:
        print(f"   !! {fc}")
    verdict = "PASS (eligible for default-ON)" if not false_credits else "FAIL"
    print(f"GATE: {verdict}")


def VETO_DATA_FOR(concept_key: str):
    want = CONCEPT_VETO[concept_key]
    return [(code, spec) for code, spec in VETO.items() if spec["concept"] == want]


# RESULTS §2 smoke pairs — clear paraphrase, inverse misconception, indirect, unrelated.
SMOKE = [
    (
        "the fluid speeds up where the pipe narrows",
        "velocity increases as cross-sectional area decreases",
        "entailment",
    ),
    (
        "pressure rises when the fluid moves faster",
        "pressure decreases as velocity increases",
        "contradiction",
    ),
    ("assume the flow does not change over time", "the flow is steady", "neutral-ish"),
    ("the pipe is painted blue", "the flow is incompressible", "unrelated"),
]


if __name__ == "__main__":
    main()
