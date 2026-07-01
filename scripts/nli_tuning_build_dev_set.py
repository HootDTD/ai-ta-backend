#!/usr/bin/env python3
"""Build the expanded NLI threshold-tuning dev set from the REAL corpus.

Authored student premises are keyed by ``entity_key`` / misconception ``code``;
the reference/misconception surface texts (hypotheses) are pulled from the live
problem JSON so they are byte-identical to what production feeds the NLI tier.

Emits two artifacts into the experiment dir:

  dev_set_reference.jsonl   -- {premise, hypothesis, gold, kind, node, group}
      Pair-level set for the reference-credit sweep (calibration.sweep_thresholds).
      Faithful because production reference candidates expose a SINGLE surface
      (the label); the shortlist has nothing else to pick.

  dev_set_veto.json         -- [{premise, code, concept, gold, kind}]
      Veto set evaluated Layer-2 (real shortlist over the concept's misconception
      surfaces + NLI), because the veto shortlists over display_name + every
      trigger_phrase and picks the best-matching surface per candidate.

Gold convention (mirrors match_nli_semantic / calibration.py):
  entailment  -> student statement SHOULD resolve/credit to this hypothesis
  neutral     -> should NOT credit (different claim)
  contradiction -> should NOT credit (opposite claim)

Run from ai-ta-backend/:
  .venv\\Scripts\\python.exe scripts\\nli_tuning_build_dev_set.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SUBJECTS = ROOT / "apollo" / "subjects"
OUT_DIR = ROOT / "docs" / "_archive" / "experiments" / "2026-07-01-nli-threshold-tuning"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Problem groups = the closed candidate set that competes together in one
# resolution. Hard negatives are drawn WITHIN a group (the realistic shortlist
# competition). Cross-group pairs never co-occur in production, so they are
# excluded (they would inflate precision artificially).
# ---------------------------------------------------------------------------
GROUPS: dict[str, tuple[str, str, list[str]]] = {
    # group_id: (concept_path, problem_file, [entity_keys competing])
    "bern_p01": (
        "fluid_mechanics/concepts/bernoulli_principle",
        "problem_01.json",
        [
            "cond.incompressibility",
            "simp.horizontal_simplification",
            "proc.plan_apply_continuity",
            "proc.plan_apply_horizontal_simplification",
            "proc.plan_solve_bernoulli_for_p2",
        ],
    ),
    "bern_p02": (
        "fluid_mechanics/concepts/bernoulli_principle",
        "problem_02.json",
        [
            "simp.equal_pressure_simplification",
            "proc.plan_apply_equal_pressure_simplification",
            "proc.plan_set_v1_zero_and_solve_bernoulli",
        ],
    ),
    "bern_p03": (
        "fluid_mechanics/concepts/bernoulli_principle",
        "problem_03.json",
        [
            "cond.incompressibility",
            "proc.plan_invoke_incompressibility",
            "proc.plan_solve_continuity_for_v2",
        ],
    ),
    "bern_p04": (
        "fluid_mechanics/concepts/bernoulli_principle",
        "problem_04.json",
        ["proc.plan_apply_flow_rate_definition"],
    ),
    "bern_p05": (
        "fluid_mechanics/concepts/bernoulli_principle",
        "problem_05.json",
        [
            "cond.incompressibility",
            "proc.plan_apply_continuity_for_v2",
            "proc.plan_substitute_into_bernoulli",
        ],
    ),
    "gdp_p01": (
        "macroeconomics/concepts/gdp_components",
        "problem_01.json",
        ["cond.final_goods_only", "proc.compute_net_exports", "proc.sum_components"],
    ),
    "gdp_p02": (
        "macroeconomics/concepts/gdp_components",
        "problem_02.json",
        ["cond.trade_deficit", "proc.subtract_imports"],
    ),
    "gdp_p03": (
        "macroeconomics/concepts/gdp_components",
        "problem_03.json",
        ["def.depreciation", "proc.compute_gnp", "proc.subtract_depreciation"],
    ),
    "nvr_p01": (
        "macroeconomics/concepts/nominal_vs_real_gdp",
        "problem_01.json",
        ["simp.deflator_is_price_index", "proc.rearrange_for_real_gdp"],
    ),
    "nvr_p02": (
        "macroeconomics/concepts/nominal_vs_real_gdp",
        "problem_02.json",
        ["def.real_basis", "proc.compute_real_change", "proc.apply_percent_change"],
    ),
}

# Concept -> which group ids belong to it (for misconception cross-negatives).
CONCEPT_GROUPS: dict[str, list[str]] = {
    "bernoulli": ["bern_p01", "bern_p02", "bern_p03", "bern_p04", "bern_p05"],
    "gdp": ["gdp_p01", "gdp_p02", "gdp_p03"],
    "nvr": ["nvr_p01", "nvr_p02"],
}

# Curated same-type, KNOWN-DISTINCT negative pairs across groups. Production
# never co-locates these in one problem's candidate set, but they are the same
# NODE TYPE and semantically distinct, so they give conditions/simplifications/
# definitions (which are singletons within their own group and thus have no
# same-group same-type sibling) genuine negative coverage. Each is asserted
# distinct by hand — NO near-duplicates here (cf. the continuity procedure steps
# that repeat across bernoulli problems, which is why cross-group PROCEDURE
# negatives are deliberately NOT auto-generated).
CURATED_CROSS_NEG: list[tuple[str, str]] = [
    ("cond.final_goods_only", "cond.trade_deficit"),          # both conditions, gdp
    ("simp.horizontal_simplification", "simp.equal_pressure_simplification"),  # both simps, bernoulli
    ("def.depreciation", "def.real_basis"),                   # both definitions (gdp / nvr)
]

# ---------------------------------------------------------------------------
# Authored student premises (positives). 2 faithful paraphrases per node:
# a "clean" one (moderate lexical divergence) and a "hard" one (indirect /
# idiomatic). Keyed by entity_key. cond.incompressibility appears in 3 groups
# but the label is identical, so it is authored once.
# ---------------------------------------------------------------------------
PREMISES: dict[str, list[str]] = {
    # --- fluid_mechanics / bernoulli ---
    "cond.incompressibility": [
        "we assume the fluid is incompressible, so its density stays the same throughout the pipe",
        "the liquid keeps a constant density as it flows, which is the incompressibility assumption",
    ],
    "simp.horizontal_simplification": [
        "since the pipe is horizontal both points sit at the same height, so the gravity potential-energy terms drop out",
        "there is no elevation change between the two sections, so the rho g h terms cancel in the energy balance",
    ],
    "proc.plan_apply_continuity": [
        "use the continuity equation with the known areas and inlet velocity to find v2 at the outlet",
        "apply conservation of mass to relate the flow speeds at the two openings and solve for the outlet velocity",
    ],
    "proc.plan_apply_horizontal_simplification": [
        "because the pipe is horizontal, set the two heights equal so the gravity terms cancel in Bernoulli's equation",
        "with no height difference between the sections, drop the potential-energy terms out of Bernoulli",
    ],
    "proc.plan_solve_bernoulli_for_p2": [
        "plug the known numbers into the simplified Bernoulli equation and solve for the pressure P2 at section 2",
        "substitute everything we know into the reduced Bernoulli relation to get the downstream pressure P2",
    ],
    "simp.equal_pressure_simplification": [
        "both ends are open to the air so the pressures are equal and the pressure terms cancel out of Bernoulli",
        "since atmospheric pressure acts at both openings, P1 and P2 are equal and drop out of the equation",
    ],
    "proc.plan_apply_equal_pressure_simplification": [
        "note that both ends are at atmospheric pressure, so the pressure terms cancel in Bernoulli's equation",
        "recognize the two openings sit at the same atmospheric pressure, so those pressure terms cancel",
    ],
    "proc.plan_set_v1_zero_and_solve_bernoulli": [
        "treat the reservoir as wide so the inlet velocity is zero, then solve the simplified Bernoulli for v2",
        "assume the top surface barely moves so v1 is zero, and solve for the exit velocity",
    ],
    "proc.plan_invoke_incompressibility": [
        "invoke the incompressibility assumption so the continuity equation reduces to A1 v1 equals A2 v2",
        "because density cancels for an incompressible fluid, continuity becomes area times velocity equal on both sides",
    ],
    "proc.plan_solve_continuity_for_v2": [
        "put the known cross-sectional areas and inlet speed into the continuity equation and solve for v2",
        "use mass conservation with the given areas and entry velocity to compute the outlet velocity",
    ],
    "proc.plan_apply_flow_rate_definition": [
        "compute the volumetric flow rate as Q equals area times velocity using the given values",
        "multiply the pipe's cross-sectional area by the flow speed to get the volume flow rate Q",
    ],
    "proc.plan_apply_continuity_for_v2": [
        "apply the continuity equation with the known areas and inlet velocity to solve for the outlet velocity v2",
        "use conservation of mass across the two sections to find the velocity where the pipe narrows",
    ],
    "proc.plan_substitute_into_bernoulli": [
        "substitute the known quantities into Bernoulli's equation and solve for the pressure at the narrow section",
        "plug the velocities and heights into Bernoulli to find P2 where the pipe narrows",
    ],
    # --- macroeconomics / gdp_components ---
    "cond.final_goods_only": [
        "only final goods and services are counted, not intermediate inputs",
        "we tally just finished products sold to end users and leave out intermediate and second-hand goods",
    ],
    "proc.compute_net_exports": [
        "compute net exports as exports minus imports",
        "take the trade balance by subtracting what the country buys abroad from what it sells abroad",
    ],
    "proc.sum_components": [
        "add up consumption, investment, government spending, and net exports to get GDP",
        "GDP under the expenditure approach is the total of C, I, G, and net exports",
    ],
    "cond.trade_deficit": [
        "imports are greater than exports, so net exports are negative and there is a trade deficit",
        "the country buys more from abroad than it sells, giving a negative trade balance",
    ],
    "proc.subtract_imports": [
        "subtract imports from exports to get net exports, then check the sign of the result",
        "find the trade balance as exports minus imports and see whether it is positive or negative",
    ],
    "def.depreciation": [
        "depreciation is the capital worn out during the year, and subtracting it from a gross figure gives the net figure",
        "the value of capital used up over the year is depreciation, and removing it turns a gross measure into a net one",
    ],
    "proc.compute_gnp": [
        "add net foreign income to GDP to obtain gross national product",
        "GNP equals GDP plus the net income residents earn from abroad",
    ],
    "proc.subtract_depreciation": [
        "subtract depreciation from GNP to obtain net national product",
        "get net national product by removing capital consumption from gross national product",
    ],
    # --- macroeconomics / nominal_vs_real_gdp ---
    "simp.deflator_is_price_index": [
        "treat the given price index as the GDP deflator so we can solve the deflator definition for real GDP",
        "assume the quoted price index is the deflator itself, which lets us back out real GDP",
    ],
    "proc.rearrange_for_real_gdp": [
        "rearrange the deflator definition so real GDP equals nominal GDP divided by PI over 100 and plug in the values",
        "solve the deflator equation for real GDP by dividing nominal GDP by the price index over one hundred",
    ],
    "def.real_basis": [
        "real GDP is adjusted for inflation, so a change in real GDP reflects a change in the quantity produced",
        "because real GDP holds prices constant, its movement shows the change in actual output rather than prices",
    ],
    "proc.compute_real_change": [
        "find the absolute change in real GDP by subtracting the earlier value from the later value",
        "take the later real GDP minus the earlier real GDP to get the raw change in output",
    ],
    "proc.apply_percent_change": [
        "convert the change to a percentage by dividing by the earlier value and multiplying by one hundred",
        "express the growth as a percent by dividing the change by the starting value and scaling by 100",
    ],
}

# ---------------------------------------------------------------------------
# Veto premises (evaluated Layer-2). Per misconception:
#   veto_positive  -> student voices the misconception (paraphrased) -> SHOULD veto
#   veto_negative  -> student states the correct opposite claim      -> must NOT veto
# ---------------------------------------------------------------------------
VETO: dict[str, dict] = {
    "misc.pressure_velocity_same_direction": {
        "concept": "bernoulli",
        "veto_positive": [
            "the fluid pressure rises as the flow moves faster",
            "when the flow speeds up, the pressure climbs higher too",
        ],
        "veto_negative": [
            "as the fluid speeds up its pressure actually drops",
            "faster flow goes with lower pressure, not higher",
        ],
    },
    "misc.density_ignored": {
        "concept": "bernoulli",
        "veto_positive": [
            "we can just ignore the fluid's density in these equations",
            "the density does not really matter here so we can leave rho out",
        ],
        "veto_negative": [
            "the density stays constant and is needed throughout the calculation",
            "we must keep rho because the fluid is incompressible with fixed density",
        ],
    },
    "misc.includes_transfers": {
        "concept": "gdp",
        "veto_positive": [
            "welfare payments and used-car sales should be added into GDP",
            "transfer payments like social security count toward GDP",
        ],
        "veto_negative": [
            "transfer payments and used goods are excluded from GDP",
            "we only count newly produced final goods, not transfers or resales",
        ],
    },
    "misc.gross_for_net": {
        "concept": "gdp",
        "veto_positive": [
            "there is no need to subtract depreciation, gross and net are the same",
            "GNP already equals NNP so we can skip depreciation",
        ],
        "veto_negative": [
            "you must subtract depreciation from GNP to get NNP",
            "net national product is gross national product minus capital consumption",
        ],
    },
    "misc.nominal_for_real": {
        "concept": "nvr",
        "veto_positive": [
            "nominal GDP is basically the same as real GDP so there is no need to adjust for inflation",
            "just use nominal GDP, the inflation adjustment does not matter",
        ],
        "veto_negative": [
            "you have to deflate nominal GDP for inflation to get real GDP",
            "real and nominal GDP differ because real GDP strips out price changes",
        ],
    },
    "misc.deflate_wrong_direction": {
        "concept": "nvr",
        "veto_positive": [
            "to get real GDP you multiply nominal GDP by the price index",
            "scale nominal GDP up by the deflator to find real GDP",
        ],
        "veto_negative": [
            "real GDP is nominal GDP divided by the price index over one hundred",
            "you deflate by dividing nominal GDP by the deflator, not multiplying",
        ],
    },
}


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _steps_for_group(concept_path: str, problem_file: str) -> dict[str, tuple[str, str]]:
    """entity_key -> (entry_type, label) for every labeled reference step."""
    prob = _load(SUBJECTS / concept_path / "problems" / problem_file)
    out: dict[str, tuple[str, str]] = {}
    for step in prob.get("reference_solution", []):
        content = step.get("content", {}) or {}
        label = content.get("label")
        if label:
            out[step["entity_key"]] = (step["entry_type"], label)
    return out


def build_reference_pairs() -> list[dict]:
    """Positives + SAME-TYPE within-group hard negatives + curated same-type
    cross-group negatives. All negatives respect the HARD type constraint
    (structural.type_compatible: student_node_type == candidate.node_type), so
    every pair could actually occur in production's NLI competition.

    Misconceptions are NOT paired against references here — they are handled in
    the veto Layer-2 evaluation and the end-to-end false-credit gate."""
    pairs: list[dict] = []
    group_steps: dict[str, dict[str, tuple[str, str]]] = {}
    # Global registry (key -> (entry_type, label)) for curated cross-group pairs.
    reg: dict[str, tuple[str, str]] = {}
    for gid, (cpath, pfile, keys) in GROUPS.items():
        steps = _steps_for_group(cpath, pfile)
        for k in keys:
            if k not in steps:
                raise SystemExit(f"[FATAL] {gid}: entity_key {k!r} has no label in {pfile}")
            reg.setdefault(k, steps[k])
        group_steps[gid] = steps

    # 1) Positives: each authored premise vs its OWN label = entailment.
    for gid, (_cpath, _pfile, keys) in GROUPS.items():
        steps = group_steps[gid]
        for key in keys:
            etype, label = steps[key]
            for prem in PREMISES.get(key, []):
                pairs.append(
                    {"premise": prem, "hypothesis": label, "gold": "entailment",
                     "kind": "positive", "node": key, "type": etype, "group": gid}
                )

    # 2) Within-group SAME-TYPE hard negatives: premise of A vs label of sibling
    #    B where entry_type(A) == entry_type(B), B != A. This is production's real
    #    shortlist competition (same problem, same type).
    for gid, (_cpath, _pfile, keys) in GROUPS.items():
        steps = group_steps[gid]
        for a in keys:
            ta, _ = steps[a]
            for b in keys:
                if a == b:
                    continue
                tb, lb = steps[b]
                if ta != tb:
                    continue  # type-incompatible: never competes in production
                for prem in PREMISES.get(a, []):
                    pairs.append(
                        {"premise": prem, "hypothesis": lb, "gold": "neutral",
                         "kind": "hard_negative", "node": f"{a}!={b}", "type": ta,
                         "group": gid}
                    )

    # 3) Curated same-type, known-distinct cross-group negatives (both directions)
    #    — coverage for singleton conditions/simplifications/definitions.
    for a, b in CURATED_CROSS_NEG:
        ta, _la = reg[a]
        tb, lb = reg[b]
        assert ta == tb, f"curated pair {a}/{b} not same type ({ta} vs {tb})"
        for src, dst in ((a, b), (b, a)):
            tsrc, _ = reg[src]
            _, ldst = reg[dst]
            for prem in PREMISES.get(src, []):
                pairs.append(
                    {"premise": prem, "hypothesis": ldst, "gold": "neutral",
                     "kind": "curated_negative", "node": f"{src}!={dst}", "type": tsrc,
                     "group": "cross"}
                )
    return pairs


def build_veto_records() -> list[dict]:
    recs: list[dict] = []
    for code, spec in VETO.items():
        for prem in spec["veto_positive"]:
            recs.append(
                {"premise": prem, "code": code, "concept": spec["concept"],
                 "gold": "entailment", "kind": "veto_positive"}
            )
        for prem in spec["veto_negative"]:
            recs.append(
                {"premise": prem, "code": code, "concept": spec["concept"],
                 "gold": "neutral", "kind": "veto_negative"}
            )
    return recs


def main() -> None:
    ref_pairs = build_reference_pairs()
    veto_recs = build_veto_records()

    ref_path = OUT_DIR / "dev_set_reference.jsonl"
    with open(ref_path, "w", encoding="utf-8") as f:
        for p in ref_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    veto_path = OUT_DIR / "dev_set_veto.json"
    with open(veto_path, "w", encoding="utf-8") as f:
        json.dump(veto_recs, f, indent=2, ensure_ascii=False)

    # Summary
    from collections import Counter

    ref_kinds = Counter(p["kind"] for p in ref_pairs)
    ref_gold = Counter(p["gold"] for p in ref_pairs)
    veto_kinds = Counter(r["kind"] for r in veto_recs)
    print(f"Reference pairs: {len(ref_pairs)}  kinds={dict(ref_kinds)}  gold={dict(ref_gold)}")
    print(f"  -> {ref_path}")
    print(f"Veto records:    {len(veto_recs)}  kinds={dict(veto_kinds)}")
    print(f"  -> {veto_path}")


if __name__ == "__main__":
    main()
