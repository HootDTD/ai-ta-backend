#!/usr/bin/env python3
"""NLI Grading Probe — real-model comparison for the Apollo NLI resolver tier.

Measures how the NLI tier changes RESOLUTION on real Apollo problems
(Bernoulli, Econ A, Econ B) by running resolve_attempt with:
  - nli_ctx=None  (NLI OFF — deterministic lexical tiers only)
  - nli_ctx=real  (NLI ON  — cross-encoder/nli-deberta-v3-small)

Per student node: token overlap, OFF/ON resolution, raw NLI scores.
Per problem: NLI-eligible count, unresolved_rate OFF→ON, recovered, vetoed,
false credits.

Run from ai-ta-backend/ with:
  .venv\\Scripts\\python.exe scripts\\nli_grading_probe.py

Writes:
  docs/_archive/experiments/2026-06-30-nli-grading-probe/resolution_results.json
  docs/_archive/experiments/2026-06-30-nli-grading-probe/resolution_draft.md
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from rapidfuzz import fuzz  # noqa: E402 - after path setup

from apollo.grading.abstention import unresolved_rate_of  # noqa: E402
from apollo.ontology.graph import KGGraph  # noqa: E402
from apollo.ontology.nodes import build_node  # noqa: E402
from apollo.resolution.candidates import (  # noqa: E402
    NLI_NODE_TYPES,
    build_candidate_set,
    candidates_from_misconceptions,
    candidates_from_reference_solution,
)
from apollo.resolution.embedding import CandidateEmbeddingCache  # noqa: E402
from apollo.resolution.nli_adjudicator import TransformersNLIAdjudicator  # noqa: E402
from apollo.resolution.nli_config import NLI_DEVICE, NLI_MODEL_SMALL, load_nli_params  # noqa: E402
from apollo.resolution.nli_resolution import NLIContext  # noqa: E402
from apollo.resolution.resolver import resolve_attempt  # noqa: E402
from apollo.resolution.semantic_shortlist import (  # noqa: E402
    shortlist_semantic_candidates,
)
from apollo.resolution.tiers import student_surface_text  # noqa: E402

SUBJECTS = ROOT / "apollo" / "subjects"
OUT_DIR = ROOT / "docs" / "_archive" / "experiments" / "2026-06-30-nli-grading-probe"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# NLI context — built once, shared across all problems
# ---------------------------------------------------------------------------

# Model is selectable via env so the same probe produces a clean small-vs-large
# comparison. Defaults to the production small model.
PROBE_MODEL = os.environ.get("NLI_PROBE_MODEL", NLI_MODEL_SMALL)
MODEL_SLUG = PROBE_MODEL.split("/")[-1]

print(f"Loading NLI model ({PROBE_MODEL})...", flush=True)
_adj = TransformersNLIAdjudicator(PROBE_MODEL, device=NLI_DEVICE)
_params = load_nli_params()
NLI_CTX = NLIContext(
    nli=_adj,
    embedder=None,  # lexical Jaccard shortlist — no OpenAI key needed
    cache=CandidateEmbeddingCache(),
    params=_params,
)
print(f"Model loaded: {PROBE_MODEL}  device={NLI_DEVICE}", flush=True)
print(
    f"NLI params: min_entailment={_params.min_entailment}  "
    f"max_contradiction={_params.max_contradiction}  "
    f"misconception_veto_entailment={_params.misconception_veto_entailment}  "
    f"top_k={_params.top_k}",
    flush=True,
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class NodeResult:
    node_id: str
    intended_key: str
    student_surface: str
    candidate_display: str
    # How lexically divergent is the student surface from the candidate display?
    token_set_ratio_vs_display: float
    # Does the student surface share ≥1 content token (len>2) with the
    # candidate display — the NLI tier's hard prerequisite?
    content_token_floor_passes: bool
    # NLI-eligible node type?
    nli_eligible_type: bool
    # OFF (nli_ctx=None)
    off_resolved: bool
    off_method: str
    off_resolved_key: str | None
    off_confidence: float
    # ON (real model)
    on_resolved: bool
    on_method: str
    on_resolved_key: str | None
    on_confidence: float
    # Raw NLI scores for this pair (computed directly; None if not applicable)
    nli_premise: str | None
    nli_hypothesis: str | None
    nli_entailment: float | None
    nli_contradiction: float | None
    nli_neutral: float | None
    nli_label: str | None
    # Derived flags
    is_control: bool
    is_misconception_paraphrase: bool
    recovered_by_nli: bool  # OFF=unresolved, ON=resolved via method=='nli'
    false_credit: bool  # ON=resolved to WRONG key (non-control, non-misc)
    veto_fired: bool  # misconception paraphrase: veto threshold crossed
    notes: str


@dataclass
class ProbeResult:
    problem_id: str
    nli_eligible_ref_count: int  # reference nodes of NLI-eligible type
    total_student_nodes: int
    unresolved_rate_off: float
    unresolved_rate_on: float
    recovered_by_nli: int
    misconception_veto_fired: bool
    false_credits: int
    nodes: list[NodeResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _node(node_id: str, node_type: str, content: dict) -> object:
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content=content,
    )


def _graph(nodes: list) -> KGGraph:
    return KGGraph(nodes=list(nodes), edges=[])


def _tsr(a: str, b: str) -> float:
    return round(fuzz.token_set_ratio(a, b) / 100.0, 4)


def _content_tokens(t: str) -> set[str]:
    return {w.strip(".,;:!?").lower() for w in t.split() if len(w) > 2}


def _floor_passes(student_text: str, candidate_display: str) -> bool:
    return bool(_content_tokens(student_text) & _content_tokens(candidate_display))


# ---------------------------------------------------------------------------
# Core probe runner
# ---------------------------------------------------------------------------


def run_probe(problem: dict, misc: dict, label: str, specs: list[dict]) -> ProbeResult:
    """Run OFF and ON resolve_attempt for one problem and collect results."""
    ref_cands = candidates_from_reference_solution(problem, canon_key_by_canonical_key={})
    misc_cands = candidates_from_misconceptions(misc, canon_key_by_canonical_key={})
    candidates = build_candidate_set(reference_nodes=ref_cands, misconception_entities=misc_cands)
    by_key = {c.canonical_key: c for c in candidates}

    nli_ref_count = sum(
        1 for s in problem.get("reference_solution", []) if s["entry_type"] in NLI_NODE_TYPES
    )

    nodes = [_node(sp["node_id"], sp["node_type"], sp["content"]) for sp in specs]
    graph = _graph(nodes)

    result_off = resolve_attempt(graph, candidates, nli_ctx=None)
    result_on = resolve_attempt(graph, candidates, nli_ctx=NLI_CTX)

    off_idx = {rn.node_id: rn for rn in result_off.resolved}
    on_idx = {rn.node_id: rn for rn in result_on.resolved}

    ur_off = unresolved_rate_of(result_off)
    ur_on = unresolved_rate_of(result_on)

    node_results: list[NodeResult] = []
    recovered = 0
    veto_fired_any = False
    false_credits = 0

    for sp in specs:
        nid = sp["node_id"]
        intended_key = sp.get("intended_key", "?")
        is_ctrl = sp.get("is_control", False)
        is_misc_par = sp.get("is_misconception_paraphrase", False)

        node_obj = _node(nid, sp["node_type"], sp["content"])
        surface = student_surface_text(node_obj)

        cand = by_key.get(intended_key)
        cand_display = cand.display_name if cand else intended_key
        nli_elig = sp["node_type"] in NLI_NODE_TYPES

        tsr = _tsr(surface, cand_display)
        floor = _floor_passes(surface, cand_display)

        rn_off = off_idx[nid]
        rn_on = on_idx[nid]

        rec = (
            rn_off.resolution != "resolved"
            and rn_on.resolution == "resolved"
            and rn_on.method == "nli"
        )
        if rec:
            recovered += 1

        fc = (
            not is_ctrl
            and not is_misc_par
            and rn_on.resolution == "resolved"
            and rn_on.resolved_key != intended_key
        )
        if fc:
            false_credits += 1

        # --- Direct NLI scores ----------------------------------------------
        nli_premise = nli_hyp = None
        nli_ent = nli_con = nli_neu = None
        nli_lbl = None
        veto_this = False
        notes_parts: list[str] = []

        if is_misc_par:
            # Veto test: find best misconception surface via lexical shortlist
            misc_type = tuple(
                c for c in candidates if c.is_misconception and c.node_type == sp["node_type"]
            )
            if misc_type:
                sc_list = shortlist_semantic_candidates(
                    node_obj, misc_type, top_k=_params.top_k, embedder=None, cache=None
                )
                if sc_list:
                    sc = sc_list[0]
                    nli_premise = surface
                    nli_hyp = sc.text
                    r = NLI_CTX.nli.classify(premise=surface, hypothesis=sc.text)
                    nli_ent = round(r.entailment, 4)
                    nli_con = round(r.contradiction, 4)
                    nli_neu = round(r.neutral, 4)
                    nli_lbl = r.label
                    veto_this = (
                        r.label == "entailment"
                        and r.entailment >= _params.misconception_veto_entailment
                    )
                    if veto_this:
                        veto_fired_any = True
                        notes_parts.append(
                            f"VETO FIRED ent={r.entailment:.4f} >= "
                            f"{_params.misconception_veto_entailment}"
                        )
                    else:
                        notes_parts.append(
                            f"veto did NOT fire ent={r.entailment:.4f} < "
                            f"{_params.misconception_veto_entailment}"
                        )

        elif not is_ctrl and nli_elig and cand is not None:
            # Reference paraphrase: find the intended candidate in the shortlist
            ref_type = tuple(
                c for c in candidates if not c.is_misconception and c.node_type == sp["node_type"]
            )
            sc_list = shortlist_semantic_candidates(
                node_obj, ref_type, top_k=_params.top_k, embedder=None, cache=None
            )
            # Find sc for intended key (may not be shortlisted if score is too low)
            sc_for_intended = next(
                (sc for sc in sc_list if sc.candidate.canonical_key == intended_key),
                None,
            )
            if sc_for_intended:
                nli_premise = surface
                nli_hyp = sc_for_intended.text
                r = NLI_CTX.nli.classify(premise=surface, hypothesis=sc_for_intended.text)
                nli_ent = round(r.entailment, 4)
                nli_con = round(r.contradiction, 4)
                nli_neu = round(r.neutral, 4)
                nli_lbl = r.label

            if not floor:
                notes_parts.append(
                    f"CONTENT-TOKEN FLOOR FAILS: "
                    f"student&display('{cand_display}')=empty -- "
                    "NLI tier structurally cannot certify this node"
                )
            else:
                if nli_ent is not None:
                    if (
                        nli_ent >= _params.min_entailment
                        and (nli_con or 0) <= _params.max_contradiction
                    ):
                        notes_parts.append(
                            f"NLI scores PASS threshold "
                            f"(ent={nli_ent:.4f}>={_params.min_entailment}, "
                            f"con={nli_con:.4f}<={_params.max_contradiction})"
                        )
                    else:
                        notes_parts.append(
                            f"NLI scores BELOW threshold "
                            f"(ent={nli_ent:.4f} need>={_params.min_entailment}; "
                            f"con={nli_con:.4f} need<={_params.max_contradiction})"
                        )
                else:
                    notes_parts.append(
                        f"intended key '{intended_key}' not in top-{_params.top_k} shortlist"
                    )

        node_results.append(
            NodeResult(
                node_id=nid,
                intended_key=intended_key,
                student_surface=surface,
                candidate_display=cand_display,
                token_set_ratio_vs_display=tsr,
                content_token_floor_passes=floor,
                nli_eligible_type=nli_elig,
                off_resolved=rn_off.resolution == "resolved",
                off_method=rn_off.method,
                off_resolved_key=rn_off.resolved_key,
                off_confidence=rn_off.confidence,
                on_resolved=rn_on.resolution == "resolved",
                on_method=rn_on.method,
                on_resolved_key=rn_on.resolved_key,
                on_confidence=rn_on.confidence,
                nli_premise=nli_premise,
                nli_hypothesis=nli_hyp,
                nli_entailment=nli_ent,
                nli_contradiction=nli_con,
                nli_neutral=nli_neu,
                nli_label=nli_lbl,
                is_control=is_ctrl,
                is_misconception_paraphrase=is_misc_par,
                recovered_by_nli=rec,
                false_credit=fc,
                veto_fired=veto_this,
                notes=" | ".join(notes_parts),
            )
        )

    return ProbeResult(
        problem_id=label,
        nli_eligible_ref_count=nli_ref_count,
        total_student_nodes=len(specs),
        unresolved_rate_off=round(ur_off, 4),
        unresolved_rate_on=round(ur_on, 4),
        recovered_by_nli=recovered,
        misconception_veto_fired=veto_fired_any,
        false_credits=false_credits,
        nodes=node_results,
    )


# ---------------------------------------------------------------------------
# Problem 1: Bernoulli (bernoulli_principle / problem_01)
# ---------------------------------------------------------------------------
#
# NLI-eligible reference nodes (5 total):
#   cond.incompressibility          → display "Incompressibility assumption" (HUMAN-READABLE)
#   simp.horizontal_simplification  → display "simp.horizontal_simplification" (DEGENERATE KEY)
#   proc.plan_apply_continuity      → display "proc.plan_apply_continuity"     (DEGENERATE KEY)
#   proc.plan_apply_horizontal_...  → display "proc.plan_appl..." (DEGENERATE KEY)
#   proc.plan_solve_bernoulli_for_p2 → display "proc.plan_sol..." (DEGENERATE KEY)
#
# Only cond.incompressibility can pass the NLI content-token floor.
# All 4 procedure/simplification nodes have degenerate keys → NLI CANNOT certify them.


def build_bernoulli_specs() -> list[dict]:
    return [
        # NLI paraphrase: CAN pass content-token floor (display = "Incompressibility assumption")
        # student surface INCLUDES "incompressibility" + "assumption"
        {
            "node_id": "b_nli_cond",
            "node_type": "condition",
            "content": {
                "applies_when": "the incompressibility assumption means the fluid preserves "
                "constant density as it moves through the pipe",
                "label": "",
            },
            "intended_key": "cond.incompressibility",
            "is_control": False,
            "is_misconception_paraphrase": False,
        },
        # NLI paraphrase: CANNOT pass floor (display = "simp.horizontal_simplification")
        # Demonstrates structural limitation for degenerate-key candidates.
        {
            "node_id": "b_nli_simp",
            "node_type": "simplification",
            "content": {
                "applies_when": "the conduit has no elevation change so height is the same "
                "at both cross-sections",
                "transformation": "gravity-driven head terms vanish from the energy equation",
            },
            "intended_key": "simp.horizontal_simplification",
            "is_control": False,
            "is_misconception_paraphrase": False,
        },
        # CONTROL: exact match via label → resolves in BOTH off and on
        {
            "node_id": "b_ctrl",
            "node_type": "condition",
            "content": {
                "applies_when": "density is constant",
                "label": "cond.incompressibility",
            },
            "intended_key": "cond.incompressibility",
            "is_control": True,
            "is_misconception_paraphrase": False,
        },
        # MISCONCEPTION paraphrase of misc.pressure_velocity_same_direction.
        # token_set_ratio vs all aliases: 42/26/76 (all < 90) — lexical tiers miss.
        # NLI-ON should veto (entailment >= 0.80 for the misconception).
        {
            "node_id": "b_misc",
            "node_type": "definition",
            "content": {
                "concept": "fluid pressure",
                "meaning": "climbs higher as the flow velocity increases",
            },
            "intended_key": "misc.pressure_velocity_same_direction",
            "is_control": False,
            "is_misconception_paraphrase": True,
        },
    ]


# ---------------------------------------------------------------------------
# Problem 2: Econ A (gdp_components / problem_01)
# ---------------------------------------------------------------------------
#
# NLI-eligible reference nodes (3 total):
#   cond.final_goods_only  → display "Final goods and services only" (HUMAN-READABLE)
#   proc.compute_net_exports → display "proc.compute_net_exports" (DEGENERATE KEY)
#   proc.sum_components      → display "proc.sum_components"      (DEGENERATE KEY)
#
# Only cond.final_goods_only can pass the content-token floor.


def build_econ_a_specs() -> list[dict]:
    return [
        # NLI paraphrase: CAN pass content-token floor (display = "Final goods and services only")
        # surface shares "final", "goods", "services" — content-token floor passes.
        # token_set_ratio vs display = 88.5 (below 90 alias threshold; candidate has aliases=() anyway)
        {
            "node_id": "ea_nli_cond",
            "node_type": "condition",
            "content": {
                "applies_when": "only final goods and services are tallied; intermediate inputs, "
                "second-hand sales, and transfer payments are omitted",
                "label": "",
            },
            "intended_key": "cond.final_goods_only",
            "is_control": False,
            "is_misconception_paraphrase": False,
        },
        # NLI paraphrase: CANNOT pass floor (display = "proc.compute_net_exports")
        {
            "node_id": "ea_nli_proc",
            "node_type": "procedure_step",
            "content": {
                "action": "take the gap between foreign sales and purchases to find "
                "the trade balance component",
                "purpose": "derive net exports for the expenditure formula",
            },
            "intended_key": "proc.compute_net_exports",
            "is_control": False,
            "is_misconception_paraphrase": False,
        },
        # CONTROL: exact via label → resolves in BOTH off and on
        {
            "node_id": "ea_ctrl",
            "node_type": "condition",
            "content": {
                "applies_when": "only final goods and services produced this year are counted",
                "label": "cond.final_goods_only",
            },
            "intended_key": "cond.final_goods_only",
            "is_control": True,
            "is_misconception_paraphrase": False,
        },
        # MISCONCEPTION paraphrase of misc.includes_transfers.
        # Uses "welfare disbursements" and "resale transactions" instead of trigger phrases.
        # token_set_ratio vs all aliases: max 48 (< 90) — lexical tiers miss.
        {
            "node_id": "ea_misc",
            "node_type": "definition",
            "content": {
                "concept": "welfare disbursements",
                "meaning": "and resale transactions ought to be added to the GDP "
                "expenditure tally alongside new production",
            },
            "intended_key": "misc.includes_transfers",
            "is_control": False,
            "is_misconception_paraphrase": True,
        },
    ]


# ---------------------------------------------------------------------------
# Problem 3: Econ B (nominal_vs_real_gdp / problem_02)
# ---------------------------------------------------------------------------
#
# NLI-eligible reference nodes (3 total):
#   def.real_basis           → display "def.real_basis"           (DEGENERATE KEY)
#   proc.compute_real_change → display "proc.compute_real_change" (DEGENERATE KEY)
#   proc.apply_percent_change→ display "proc.apply_percent_change"(DEGENERATE KEY)
#
# NONE can pass the content-token floor — NLI cannot recover any reference
# node in this problem given the current dataset.


def build_econ_b_specs() -> list[dict]:
    return [
        # NLI paraphrase: CANNOT pass floor (display = "def.real_basis")
        # Demonstrates that even a semantically correct definition paraphrase
        # is blocked by the degenerate-key content-token floor.
        {
            "node_id": "eb_nli_def",
            "node_type": "definition",
            "content": {
                "concept": "real GDP",
                "meaning": "strips out inflation so it captures only the volume increase "
                "in goods and services produced",
            },
            "intended_key": "def.real_basis",
            "is_control": False,
            "is_misconception_paraphrase": False,
        },
        # NLI paraphrase: CANNOT pass floor (display = "proc.compute_real_change")
        {
            "node_id": "eb_nli_proc",
            "node_type": "procedure_step",
            "content": {
                "action": "find the gap between the later and earlier output figures to "
                "isolate the absolute real change",
                "purpose": "measure the change in quantity of output before converting to a percentage",
            },
            "intended_key": "proc.compute_real_change",
            "is_control": False,
            "is_misconception_paraphrase": False,
        },
        # CONTROL: exact alias match for misc.nominal_for_real
        # surface = "nominal gdp is the same as real gdp" — exact alias → method='alias'
        {
            "node_id": "eb_ctrl",
            "node_type": "definition",
            "content": {
                "concept": "nominal",
                "meaning": "gdp is the same as real gdp",
            },
            "intended_key": "misc.nominal_for_real",
            "is_control": True,
            "is_misconception_paraphrase": False,
        },
        # MISCONCEPTION paraphrase of misc.nominal_for_real.
        # Uses "current dollar output" / "inflation-corrected" — no literal trigger phrases.
        # token_set_ratio vs all aliases: max 34 (< 90) — lexical tiers miss.
        # NLI-ON veto should fire.
        {
            "node_id": "eb_misc",
            "node_type": "definition",
            "content": {
                "concept": "current dollar output",
                "meaning": "tells us as much as the inflation-corrected figure would "
                "since prices preserve the production total",
            },
            "intended_key": "misc.nominal_for_real",
            "is_control": False,
            "is_misconception_paraphrase": True,
        },
    ]


# ---------------------------------------------------------------------------
# Run all three probes
# ---------------------------------------------------------------------------


def main() -> None:
    print("\n=== PROBLEM 1: Bernoulli (bernoulli_principle/problem_01) ===", flush=True)
    bern_problem = _load(
        SUBJECTS / "fluid_mechanics/concepts/bernoulli_principle/problems/problem_01.json"
    )
    bern_misc = _load(SUBJECTS / "fluid_mechanics/concepts/bernoulli_principle/misconceptions.json")
    result_bern = run_probe(bern_problem, bern_misc, "bernoulli_01", build_bernoulli_specs())
    _print_summary(result_bern)

    print("\n=== PROBLEM 2: Econ A (gdp_components/problem_01) ===", flush=True)
    econ_a_problem = _load(
        SUBJECTS / "macroeconomics/concepts/gdp_components/problems/problem_01.json"
    )
    econ_a_misc = _load(SUBJECTS / "macroeconomics/concepts/gdp_components/misconceptions.json")
    result_econ_a = run_probe(
        econ_a_problem, econ_a_misc, "econ_a_gdp_components_01", build_econ_a_specs()
    )
    _print_summary(result_econ_a)

    print("\n=== PROBLEM 3: Econ B (nominal_vs_real_gdp/problem_02) ===", flush=True)
    econ_b_problem = _load(
        SUBJECTS / "macroeconomics/concepts/nominal_vs_real_gdp/problems/problem_02.json"
    )
    econ_b_misc = _load(
        SUBJECTS / "macroeconomics/concepts/nominal_vs_real_gdp/misconceptions.json"
    )
    result_econ_b = run_probe(
        econ_b_problem, econ_b_misc, "econ_b_real_gdp_02", build_econ_b_specs()
    )
    _print_summary(result_econ_b)

    # --- Serialise to JSON --------------------------------------------------
    all_results = [result_bern, result_econ_a, result_econ_b]
    json_path = OUT_DIR / f"resolution_results_{MODEL_SLUG}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    **{k: v for k, v in asdict(r).items() if k != "nodes"},
                    "nodes": [asdict(n) for n in r.nodes],
                }
                for r in all_results
            ],
            f,
            indent=2,
        )
    print(f"\nResults written -> {json_path}", flush=True)

    # --- Write markdown draft -----------------------------------------------
    md_path = OUT_DIR / f"resolution_draft_{MODEL_SLUG}.md"
    _write_markdown(all_results, md_path)
    print(f"Draft written  -> {md_path}", flush=True)

    # --- Final summary table ------------------------------------------------
    print("\n=== FINAL SUMMARY ===")
    print(
        f"{'Problem':<35} {'NLI-elig':>8} {'UR OFF':>8} {'UR ON':>8} "
        f"{'Recovered':>9} {'Vetoed':>7} {'FalseCredit':>12}"
    )
    for r in all_results:
        print(
            f"{r.problem_id:<35} {r.nli_eligible_ref_count:>8} "
            f"{r.unresolved_rate_off:>8.4f} {r.unresolved_rate_on:>8.4f} "
            f"{r.recovered_by_nli:>9} {str(r.misconception_veto_fired):>7} "
            f"{r.false_credits:>12}"
        )


def _print_summary(r: ProbeResult) -> None:
    print(f"  NLI-eligible ref nodes : {r.nli_eligible_ref_count}")
    print(f"  unresolved_rate OFF    : {r.unresolved_rate_off:.4f}")
    print(f"  unresolved_rate ON     : {r.unresolved_rate_on:.4f}")
    print(f"  recovered by NLI       : {r.recovered_by_nli}")
    print(f"  misconception vetoed   : {r.misconception_veto_fired}")
    print(f"  false credits          : {r.false_credits}")
    for n in r.nodes:
        tag = "[CTRL]" if n.is_control else ("[MISC]" if n.is_misconception_paraphrase else "[NLI]")
        print(
            f"    {tag} {n.node_id}: OFF={n.off_method}->{n.off_resolved_key} | "
            f"ON={n.on_method}->{n.on_resolved_key}",
            end="",
        )
        if n.nli_entailment is not None:
            print(
                f" | NLI ent={n.nli_entailment:.4f} con={n.nli_contradiction:.4f} "
                f"lbl={n.nli_label}",
                end="",
            )
        if n.notes:
            print(f" | {n.notes}", end="")
        print()


def _write_markdown(results: list[ProbeResult], path: Path) -> None:
    lines: list[str] = [
        "# NLI Grading Probe — Resolution Results",
        "",
        f"**Date:** 2026-06-30  **Model:** `{PROBE_MODEL}`  **Device:** `{NLI_DEVICE}`",
        "",
        "**NLI params:**",
        f"- `min_entailment` = {_params.min_entailment}",
        f"- `max_contradiction` = {_params.max_contradiction}",
        f"- `misconception_veto_entailment` = {_params.misconception_veto_entailment}",
        f"- `ambiguity_margin` = {_params.ambiguity_margin}",
        f"- `top_k` = {_params.top_k}",
        "",
        "## Summary",
        "",
        "| Problem | NLI-elig | UR OFF | UR ON | Recovered | Vetoed | FalseCredit |",
        "|---------|----------|--------|-------|-----------|--------|-------------|",
    ]
    for r in results:
        lines.append(
            f"| {r.problem_id} | {r.nli_eligible_ref_count} | "
            f"{r.unresolved_rate_off:.4f} | {r.unresolved_rate_on:.4f} | "
            f"{r.recovered_by_nli} | {r.misconception_veto_fired} | "
            f"{r.false_credits} |"
        )

    for r in results:
        lines += [
            "",
            f"## Problem: `{r.problem_id}`",
            "",
            "| node_id | type | intended_key | student_surface (truncated) | "
            "TSR-vs-display | floor | OFF method→key | ON method→key | "
            "NLI ent | NLI con | label | notes |",
            "|---------|------|-------------|----------------------------|"
            "---------------|-------|----------------|---------------|"
            "---------|---------|-------|-------|",
        ]
        for n in r.nodes:
            surface_trunc = n.student_surface[:60].replace("|", "/")
            off_k = (n.off_resolved_key or "–")[:25]
            on_k = (n.on_resolved_key or "–")[:25]
            lines.append(
                f"| {n.node_id} | {n.intended_key.split('.')[0]} | "
                f"`{n.intended_key}` | {surface_trunc}… | "
                f"{n.token_set_ratio_vs_display:.3f} | "
                f"{'Y' if n.content_token_floor_passes else 'N'} | "
                f"{n.off_method}→{off_k} | {n.on_method}→{on_k} | "
                f"{n.nli_entailment if n.nli_entailment is not None else '–'} | "
                f"{n.nli_contradiction if n.nli_contradiction is not None else '–'} | "
                f"{n.nli_label or '–'} | {n.notes[:80] if n.notes else '–'} |"
            )

        lines += ["", "### Node notes", ""]
        for n in r.nodes:
            tag = "CTRL" if n.is_control else ("MISC" if n.is_misconception_paraphrase else "NLI")
            lines.append(
                f"- **{n.node_id}** [{tag}] floor={'PASS' if n.content_token_floor_passes else 'FAIL'} "
                f"recovered={n.recovered_by_nli} false_credit={n.false_credit} "
                f"veto={n.veto_fired}"
            )
            if n.nli_premise:
                lines.append(f"  - NLI premise:     `{n.nli_premise[:120]}`")
                lines.append(f"  - NLI hypothesis:  `{n.nli_hypothesis[:120]}`")
                lines.append(
                    f"  - Scores: ent={n.nli_entailment} con={n.nli_contradiction} "
                    f"neu={n.nli_neutral} → label=**{n.nli_label}**"
                )
            if n.notes:
                lines.append(f"  - Notes: {n.notes}")

    lines += [
        "",
        "## Key Findings",
        "",
        "1. **Content-token floor structural limitation**: Reference candidates whose "
        "`display_name` is the canonical key (e.g. `proc.compute_net_exports`, "
        "`simp.horizontal_simplification`, `def.real_basis`) have a single-token "
        "display surface that student paraphrases cannot share content tokens with. "
        "The NLI tier's `_content_tokens(student) & _content_tokens(sc.text) = empty` "
        "guard blocks NLI before it ever calls the model for these nodes.",
        "",
        "2. **Human-readable display names enable NLI recovery**: Only candidates with "
        "human-readable labels (e.g. `cond.incompressibility` → "
        '"Incompressibility assumption") can pass the floor — see per-node scores above.',
        "",
        "3. **Misconception veto**: See per-problem veto_fired status above.",
        "",
        "4. **False credits**: Any node where ON resolved to the WRONG key is flagged; "
        "count above.",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
