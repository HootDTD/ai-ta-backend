"""Interactive Apollo clarification session driver — NLI off vs on.

Shows what Apollo (the confused learner) would ASK a student during a Bernoulli
teach-back, and how the NLI tier changes it: a paraphrase that NLI resolves is
no longer flagged, so Apollo asks about it in the OFF mode but NOT in the ON
mode. Uses the REAL OpenAI embedder + REAL NLI model. No DB needed (we call the
detection primitives directly, skipping the asked_waiting persistence).

Run 1 (no answers arg): prints Apollo's probe questions for OFF and ON.
Run 2 (--answer node_id=candidate_key ...): applies the student's answers as
confirmed_resolutions and prints the resulting resolution.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from apollo.clarification.detector import detect_ambiguous_nodes  # noqa: E402
from apollo.clarification.embedding import CandidateEmbeddingCache  # noqa: E402
from apollo.clarification.probe import build_probe_hint  # noqa: E402
from apollo.ontology.graph import KGGraph  # noqa: E402
from apollo.ontology.nodes import build_node  # noqa: E402
from apollo.resolution import find_residual_nodes, resolve_attempt  # noqa: E402
from apollo.resolution.candidates import (  # noqa: E402
    build_candidate_set,
    candidates_from_misconceptions,
    candidates_from_reference_solution,
)
from apollo.resolution.embedding import default_embedder  # noqa: E402
from apollo.resolution.nli_adjudicator import TransformersNLIAdjudicator  # noqa: E402
from apollo.resolution.nli_config import NLI_DEVICE, NLI_MODEL_SMALL, load_nli_params  # noqa: E402
from apollo.resolution.nli_resolution import NLIContext  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BERNOULLI = ROOT / "apollo/subjects/fluid_mechanics/concepts/bernoulli_principle"


def _candidates():
    prob = json.load(open(BERNOULLI / "problems/problem_01.json"))
    misc = json.load(open(BERNOULLI / "misconceptions.json"))
    refs = candidates_from_reference_solution(prob, canon_key_by_canonical_key={})
    miscs = candidates_from_misconceptions(misc, canon_key_by_canonical_key={})
    return build_candidate_set(reference_nodes=refs, misconception_entities=miscs)


def _student_graph() -> KGGraph:
    """A Bernoulli teach-back with three conditions the student states in their
    own words. All target `cond.incompressibility` ("Incompressibility
    assumption") — the ONE conceptual ref node that carries a real label."""
    nodes = [
        # (a) a clean paraphrase — NLI should resolve this (entails the ref).
        build_node(
            node_type="condition",
            node_id="stu_paraphrase",
            attempt_id=1,
            source="parser",
            content={
                "applies_when": "the liquid keeps the same density all the way through the pipe"
            },
        ),
        # (b) a vague, under-committed statement — near the concept but too
        # imprecise to entail; stays residual → Apollo should probe it.
        build_node(
            node_type="condition",
            node_id="stu_vague",
            attempt_id=1,
            source="parser",
            content={"applies_when": "the fluid doesn't really change as it flows"},
        ),
    ]
    return KGGraph(nodes=nodes, edges=[])


def _nli_ctx() -> NLIContext:
    return NLIContext(
        nli=TransformersNLIAdjudicator(NLI_MODEL_SMALL, device=NLI_DEVICE),
        embedder=default_embedder,
        cache=CandidateEmbeddingCache(),
        params=load_nli_params(),
    )


def _apollo_questions(graph, cands, *, nli_ctx):
    """Mirror run_clarification_detection's detection (sans DB persistence):
    residual -> ambiguity flag -> probe hint."""
    residual = find_residual_nodes(list(graph.nodes), cands, symbolic_mappings={}, nli_ctx=nli_ctx)
    flagged = detect_ambiguous_nodes(
        residual, cands, embedder=default_embedder, cache=CandidateEmbeddingCache()
    )
    return residual, flagged


def run1():
    cands = _candidates()
    graph = _student_graph()
    ctx = _nli_ctx()
    print("STUDENT TEACH-BACK (Bernoulli):")
    for n in graph.nodes:
        print(f"  [{n.node_id}] {n.content.applies_when!r}")
    for mode, kw in [("NLI OFF", None), ("NLI ON", ctx)]:
        residual, flagged = _apollo_questions(graph, cands, nli_ctx=kw)
        print(f"\n=== {mode} ===")
        print(f"  residual nodes: {[n.node_id for n in residual]}")
        if not flagged:
            print("  Apollo asks: (nothing — all teach-back nodes resolved)")
        for f in flagged:
            print(
                f"  Apollo probes [{f.node.node_id}] -> steering: {build_probe_hint(f.node, f.candidate)!r}"
            )
            print(
                f"      (nearest candidate: {f.candidate.canonical_key} / {f.candidate.display_name!r})"
            )


def run2(answers: dict[str, str]):
    cands = _candidates()
    graph = _student_graph()
    print(f"STUDENT ANSWERS (confirmed_resolutions): {answers}")
    res = resolve_attempt(graph, cands, confirmed_resolutions=answers)
    for rn in res.resolved:
        print(
            f"  [{rn.node_id}] -> {rn.resolution} method={rn.method} conf={rn.confidence} key={getattr(rn, 'resolved_key', None)}"
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer", action="append", default=[], help="node_id=candidate_key")
    args = ap.parse_args()
    if args.answer:
        run2(dict(a.split("=", 1) for a in args.answer))
    else:
        run1()
