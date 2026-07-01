#!/usr/bin/env python3
"""Per-model NLI threshold tuning — reference-credit sweep + veto sweep.

Runs one model end to end over the expanded dev set and picks the operating
point per the calibration goal: PRECISION >= floor FIRST, then MAX RECALL,
tuned SEPARATELY per model.

  Reference sweep (credit precision/recall)
    - Faithful pair-level classification (production reference candidates expose
      a single surface = the label, so premise-vs-label mirrors the shortlist).
    - Reuses the tested production apparatus (calibration.sweep_thresholds +
      best_operating_point) AND adds a FINE grid (min_ent 0.50..0.99 step 0.01)
      for a higher-resolution operating point ("same method, and more").
    - Reports per-kind false positives (the model's actual false credits) and
      recall misses at the selected point.

  Veto sweep (misconception detection)
    - Layer-2: builds each concept's misconception candidate set, forms a
      definition-type student node, and computes the MAX entailment over all
      misconception surfaces (display_name + trigger_phrases) that pass the
      production polarity gate. Embedder-independent upper bound on veto firing.
    - Sweeps misconception_veto_entailment; picks max veto-recall subject to
      ZERO false vetoes (a correct statement must never be vetoed).

Usage (from ai-ta-backend/):
  .venv\\Scripts\\python.exe scripts\\nli_tuning_sweep.py [MODEL_NAME]
    MODEL_NAME defaults to the production small model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SUBJECTS = ROOT / "apollo" / "subjects"
EXP = ROOT / "docs" / "_archive" / "experiments" / "2026-07-01-nli-threshold-tuning"

from apollo.ontology.nodes import build_node  # noqa: E402
from apollo.resolution.calibration import (  # noqa: E402
    LabeledPair,
    best_operating_point,
    sweep_thresholds,
)
from apollo.resolution.candidates import candidates_from_misconceptions  # noqa: E402
from apollo.resolution.embedding import candidate_surface_texts  # noqa: E402
from apollo.resolution.nli_adjudicator import NLIResult, TransformersNLIAdjudicator  # noqa: E402
from apollo.resolution.nli_config import NLI_DEVICE, NLI_MODEL_NAME  # noqa: E402
from apollo.resolution.polarity import polarity_allows_match  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else NLI_MODEL_NAME
SLUG = MODEL.split("/")[-1]

CONCEPT_MISC = {
    "bernoulli": "fluid_mechanics/concepts/bernoulli_principle/misconceptions.json",
    "gdp": "macroeconomics/concepts/gdp_components/misconceptions.json",
    "nvr": "macroeconomics/concepts/nominal_vs_real_gdp/misconceptions.json",
}

# Fine grids ("and more" beyond the production 0.60..0.95 / 0.05..0.20 grids).
MIN_ENT_FINE = [round(0.50 + i * 0.01, 2) for i in range(50)]  # 0.50 .. 0.99
MAX_CON_FINE = [0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 1.01]  # 1.01 == "no contradiction cap"
VETO_GRID = [round(0.50 + i * 0.01, 2) for i in range(50)]  # 0.50 .. 0.99
PRECISION_FLOOR = 0.95


# ---------------------------------------------------------------------------
# Model + memoized classify
# ---------------------------------------------------------------------------
print(f"[{SLUG}] loading model...", flush=True)
_adj = TransformersNLIAdjudicator(MODEL, device=NLI_DEVICE)
_cache: dict[tuple[str, str], NLIResult] = {}


def classify(premise: str, hypothesis: str) -> NLIResult:
    key = (premise, hypothesis)
    if key not in _cache:
        _cache[key] = _adj.classify(premise=premise, hypothesis=hypothesis)
    return _cache[key]


def _load_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _def_node(premise: str):
    return build_node(
        node_type="definition",
        node_id="stu",
        attempt_id=1,
        source="parser",
        content={"concept": "", "meaning": premise},
    )


# ---------------------------------------------------------------------------
# Reference sweep
# ---------------------------------------------------------------------------
def reference_sweep() -> dict:
    pairs = [
        json.loads(line)
        for line in (EXP / "dev_set_reference.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(f"[{SLUG}] reference: classifying {len(pairs)} pairs...", flush=True)
    for p in pairs:
        r = classify(p["premise"], p["hypothesis"])
        p["_ent"], p["_con"], p["_label"] = r.entailment, r.contradiction, r.label

    # --- Production apparatus (tested code path) ---
    labeled = [LabeledPair(p["premise"], p["hypothesis"], p["gold"]) for p in pairs]
    prod_report = sweep_thresholds(labeled, classify)  # cache hit -> no re-inference
    prod_sel = best_operating_point(prod_report, PRECISION_FLOOR)

    # --- Fine grid ---
    def metrics(min_ent: float, max_con: float):
        tp = fp = fn = 0
        for p in pairs:
            credited = p["_label"] == "entailment" and p["_ent"] >= min_ent and p["_con"] <= max_con
            if credited:
                tp += 1 if p["gold"] == "entailment" else 0
                fp += 0 if p["gold"] == "entailment" else 1
            elif p["gold"] == "entailment":
                fn += 1
        prec = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
        rec = 0.0 if (tp + fn) == 0 else tp / (tp + fn)
        return prec, rec, tp, fp, fn

    grid = []
    for me in MIN_ENT_FINE:
        for mc in MAX_CON_FINE:
            prec, rec, tp, fp, fn = metrics(me, mc)
            grid.append((me, mc, prec, rec, tp, fp, fn))
    eligible = [g for g in grid if g[2] >= PRECISION_FLOOR]
    fine_sel = max(eligible, key=lambda g: (g[3], g[0], -g[1])) if eligible else None

    # --- Diagnostics at the fine operating point ---
    fps, fns = [], []
    if fine_sel:
        me, mc = fine_sel[0], fine_sel[1]
        for p in pairs:
            credited = p["_label"] == "entailment" and p["_ent"] >= me and p["_con"] <= mc
            if credited and p["gold"] != "entailment":
                fps.append(p)
            if (not credited) and p["gold"] == "entailment":
                fns.append(p)

    return {
        "pairs": pairs,
        "prod_selected": None
        if prod_sel is None
        else {
            "min_entailment": prod_sel.min_entailment,
            "max_contradiction": prod_sel.max_contradiction,
        },
        "fine_grid": grid,
        "fine_selected": None
        if fine_sel is None
        else {
            "min_entailment": fine_sel[0],
            "max_contradiction": fine_sel[1],
            "precision": fine_sel[2],
            "recall": fine_sel[3],
            "tp": fine_sel[4],
            "fp": fine_sel[5],
            "fn": fine_sel[6],
        },
        "false_positives": fps,
        "recall_misses": fns,
    }


# ---------------------------------------------------------------------------
# Veto sweep (Layer-2)
# ---------------------------------------------------------------------------
def veto_sweep() -> dict:
    recs = _load_json(EXP / "dev_set_veto.json")
    misc_cands = {
        concept: candidates_from_misconceptions(
            _load_json(SUBJECTS / path), canon_key_by_canonical_key={}
        )
        for concept, path in CONCEPT_MISC.items()
    }
    print(f"[{SLUG}] veto: evaluating {len(recs)} records...", flush=True)
    for r in recs:
        premise = r["premise"]
        best_ent, best_surface, best_code = 0.0, None, None
        for c in misc_cands[r["concept"]]:
            for surf in candidate_surface_texts(c):
                if not polarity_allows_match(premise, surf).allowed:
                    continue
                res = classify(premise, surf)
                if res.label == "entailment" and res.entailment > best_ent:
                    best_ent, best_surface, best_code = res.entailment, surf, c.canonical_key
        r["_best_ent"] = best_ent
        r["_best_surface"] = best_surface
        r["_best_code"] = best_code

    pos = [r for r in recs if r["kind"] == "veto_positive"]
    neg = [r for r in recs if r["kind"] == "veto_negative"]

    grid = []
    for t in VETO_GRID:
        fired_pos = sum(1 for r in pos if r["_best_ent"] >= t)
        fired_neg = sum(1 for r in neg if r["_best_ent"] >= t)  # false vetoes
        recall = fired_pos / len(pos) if pos else 0.0
        prec = 1.0 if (fired_pos + fired_neg) == 0 else fired_pos / (fired_pos + fired_neg)
        grid.append((t, recall, prec, fired_pos, fired_neg))
    # max recall subject to ZERO false vetoes; tie-break lower threshold (more recall headroom)
    clean = [g for g in grid if g[4] == 0]
    sel = max(clean, key=lambda g: (g[1], -g[0])) if clean else None

    return {
        "records": recs,
        "grid": grid,
        "selected": None
        if sel is None
        else {
            "veto_threshold": sel[0],
            "recall": sel[1],
            "precision": sel[2],
            "fired_pos": sel[3],
            "false_vetoes": sel[4],
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def main() -> None:
    ref = reference_sweep()
    veto = veto_sweep()

    print("\n" + "=" * 78)
    print(f"MODEL: {MODEL}")
    print("=" * 78)

    print("\n--- REFERENCE-CREDIT SWEEP ---")
    print(f"production best_operating_point(>= {PRECISION_FLOOR}): {ref['prod_selected']}")
    fs = ref["fine_selected"]
    if fs:
        print(
            f"FINE operating point: min_ent={fs['min_entailment']:.2f} "
            f"max_con={fs['max_contradiction']:.2f}  "
            f"precision={fs['precision']:.3f} recall={fs['recall']:.3f} "
            f"(tp={fs['tp']} fp={fs['fp']} fn={fs['fn']})"
        )
    else:
        print("FINE operating point: NONE meets the precision floor.")
    n_pos = sum(1 for p in ref["pairs"] if p["gold"] == "entailment")
    print(f"positives={n_pos}  negatives={len(ref['pairs']) - n_pos}")
    if ref["false_positives"]:
        print(f"FALSE POSITIVES at op point ({len(ref['false_positives'])}):")
        for p in ref["false_positives"]:
            print(f"   [{p['kind']}] {p['node']}  ent={p['_ent']:.3f} con={p['_con']:.3f}")
            print(f"       premise: {p['premise'][:80]}")
            print(f"       hypoth : {p['hypothesis'][:80]}")
    else:
        print("FALSE POSITIVES at op point: 0")
    if ref["recall_misses"]:
        print(f"RECALL MISSES at op point ({len(ref['recall_misses'])}):")
        for p in ref["recall_misses"]:
            print(f"   {p['node']:40} ent={p['_ent']:.3f} con={p['_con']:.3f} lbl={p['_label']}")

    print("\n--- VETO SWEEP ---")
    print(f"selected: {veto['selected']}")
    print("per-record best entailment (max over misc surfaces, polarity-gated):")
    for r in veto["records"]:
        tag = "POS" if r["kind"] == "veto_positive" else "neg"
        print(f"   [{tag}] {r['code']:38} ent={r['_best_ent']:.3f}  <- {r['_best_surface']}")

    out = EXP / f"tuning_results_{SLUG}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "model": MODEL,
                "precision_floor": PRECISION_FLOOR,
                "reference": {k: v for k, v in ref.items() if k != "pairs"},
                "reference_pairs": ref["pairs"],
                "veto": veto,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\n[{SLUG}] wrote {out}")


if __name__ == "__main__":
    main()
