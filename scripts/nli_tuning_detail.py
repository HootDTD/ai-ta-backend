#!/usr/bin/env python3
"""Emit the detailed markdown sections (full fine-grid breakpoints, complete
recall-miss lists, full 24-row veto table) from the tuning result JSONs, plus
complete-grid CSVs. Writes the markdown block to a file for insertion into
RESULTS.md. No hand-transcription -> no drift from the raw numbers."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "docs" / "_archive" / "experiments" / "2026-07-01-nli-threshold-tuning"

SMALL = json.loads((EXP / "tuning_results_nli-deberta-v3-small.json").read_text(encoding="utf-8"))
LARGE = json.loads((EXP / "tuning_results_nli-deberta-v3-large.json").read_text(encoding="utf-8"))

MAX_CON_REPORT = 0.10


def recall_breakpoints(data: dict, max_con: float) -> list[tuple]:
    """Collapse the fine grid (at one max_con) into recall breakpoints:
    (min_ent_low, min_ent_high, recall, tp, fp) rows where recall is constant."""
    rows = sorted(
        [(me, rec, tp, fp) for (me, mc, prec, rec, tp, fp, fn) in data["reference"]["fine_grid"]
         if abs(mc - max_con) < 1e-9]
    )
    out = []
    i = 0
    while i < len(rows):
        me, rec, tp, fp = rows[i]
        j = i
        while j + 1 < len(rows) and abs(rows[j + 1][1] - rec) < 1e-9 and rows[j + 1][2] == tp:
            j += 1
        out.append((rows[i][0], rows[j][0], rec, tp, fp))
        i = j + 1
    return out


def maxcon_sensitivity(data: dict, min_ent: float) -> list[tuple]:
    rows = [(mc, rec, prec, tp, fp) for (me, mc, prec, rec, tp, fp, fn) in data["reference"]["fine_grid"]
            if abs(me - min_ent) < 1e-9]
    return sorted(rows)


def miss_rows(data: dict, min_ent: float, max_con: float) -> list[dict]:
    out = []
    for p in data["reference_pairs"]:
        if p["gold"] != "entailment":
            continue
        credited = p["_label"] == "entailment" and p["_ent"] >= min_ent and p["_con"] <= max_con
        if not credited:
            out.append(p)
    return out


def write_grid_csv(data: dict, name: str) -> None:
    with open(EXP / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["min_entailment", "max_contradiction", "precision", "recall", "tp", "fp", "fn"])
        for row in data["reference"]["fine_grid"]:
            w.writerow(row)


write_grid_csv(SMALL, "fine_grid_nli-deberta-v3-small.csv")
write_grid_csv(LARGE, "fine_grid_nli-deberta-v3-large.csv")

L = []
A = L.append

A("## 7. Full fine-grid detail")
A("")
A("Complete cell-by-cell grids are in `fine_grid_<model>.csv` (50 `min_ent` × 7 "
  "`max_con` = 350 rows each). Because **precision is 1.000 at every cell** and "
  "the highest negative entailment is 0.000, recall is a step function of "
  "`min_entailment` alone; the tables below give the lossless recall breakpoints "
  f"at `max_con={MAX_CON_REPORT}` (fp=0 throughout).")
A("")

for tag, data in (("small", SMALL), ("large", LARGE)):
    A(f"### 7.{'1' if tag == 'small' else '2'} {tag} — recall breakpoints (`max_con={MAX_CON_REPORT}`)")
    A("")
    A("| `min_ent` range | recall | tp/56 |")
    A("|---|---|---|")
    for lo, hi, rec, tp, fp in recall_breakpoints(data, MAX_CON_REPORT):
        rng = f"{lo:.2f}" if abs(lo - hi) < 1e-9 else f"{lo:.2f}–{hi:.2f}"
        A(f"| {rng} | {rec:.3f} | {tp} |")
    A("")

A("### 7.3 `max_contradiction` sensitivity (at each model's selected `min_ent`)")
A("")
A("| model | `min_ent` | `max_con` | recall | precision |")
A("|---|---|---|---|---|")
for tag, data, me in (("small", SMALL, 0.50), ("large", LARGE, 0.90)):
    for mc, rec, prec, tp, fp in maxcon_sensitivity(data, me):
        cap = "∞" if mc >= 1.0 else f"{mc:.2f}"
        A(f"| {tag} | {me:.2f} | {cap} | {rec:.3f} | {prec:.3f} |")
A("")
A("`max_con` behaves differently per model. For **large** it is inert — recall is "
  "0.714 at every cap (its recovered positives all have con≈0). For **small** it "
  "is a live knob *below* 0.10: tightening to 0.05 / 0.02 drops recall to "
  "0.607 / 0.589 (a few correct paraphrases carry contradiction up to ~0.10), "
  "while loosening above 0.10 adds nothing (no negative is ever labeled "
  "entailment). So `max_con=0.10` is the small-model plateau knee — the loosest "
  "cap that still admits zero negatives — and 0.05 costs the large model no recall.")
A("")

A("## 8. Complete recall-miss lists")
A("")
A("Every positive NOT credited at each model's **max-recall** operating point "
  "(`min_ent` 0.50 small / 0.90 large, `max_con=0.10`). These are the model's "
  "capability ceiling — no in-range threshold recovers them; the `contradiction` "
  "cases are where the model actively mis-reads a correct paraphrase.")
A("")

for tag, data, me in (("small", SMALL, 0.50), ("large", LARGE, 0.90)):
    misses = miss_rows(data, me, MAX_CON_REPORT)
    A(f"### 8.{'1' if tag == 'small' else '2'} {tag} — {len(misses)}/56 missed (at `min_ent={me:.2f}`)")
    A("")
    A("| node | type | ent | con | label |")
    A("|---|---|---|---|---|")
    for p in sorted(misses, key=lambda x: (x["type"], x["node"])):
        A(f"| `{p['node']}` | {p['type']} | {p['_ent']:.3f} | {p['_con']:.3f} | {p['_label']} |")
    A("")

A("## 9. Full veto record table")
A("")
A("All 24 veto records with each model's **max entailment over the concept's "
  "misconception surfaces** (polarity-gated). `fires?` is at the selected floor "
  "(small 0.96 / large 0.98). A `veto_positive` should fire; a `veto_negative` "
  "must not. Rows are joined by construction order (same dev set).")
A("")
A("| kind | misconception | small ent | small fires? | large ent | large fires? | best surface (large) |")
A("|---|---|---|---|---|---|---|")
srecs, lrecs = SMALL["veto"]["records"], LARGE["veto"]["records"]
S_FLOOR, L_FLOOR = 0.96, 0.98
for sr, lr in zip(srecs, lrecs):
    kind = "POS" if sr["kind"] == "veto_positive" else "neg"
    s_fire = "✓" if sr["_best_ent"] >= S_FLOOR else "—"
    l_fire = "✓" if lr["_best_ent"] >= L_FLOOR else "—"
    surf = (lr["_best_surface"] or "—")
    # for veto_negative a fire is a FALSE veto -> flag it
    if sr["kind"] == "veto_negative":
        s_fire = "FALSE-VETO" if sr["_best_ent"] >= S_FLOOR else "ok"
        l_fire = "FALSE-VETO" if lr["_best_ent"] >= L_FLOOR else "ok"
    A(f"| {kind} | `{sr['code']}` | {sr['_best_ent']:.3f} | {s_fire} | "
      f"{lr['_best_ent']:.3f} | {l_fire} | {surf[:42]} |")
A("")
A("Note the `neg` rows scoring 0.88–0.98 (e.g. a correct deflation statement "
  "entailing \"Uses nominal GDP as real GDP\"): these are the false-veto risks the "
  "high floors are calibrated to clear — the model's polarity/lexical-overlap "
  "errors, not a dev-set labeling issue.")
A("")

out = EXP / "_detail_sections.md"
out.write_text("\n".join(L), encoding="utf-8")
print(f"wrote {out}  ({len(L)} lines)")
print("wrote fine_grid_nli-deberta-v3-small.csv, fine_grid_nli-deberta-v3-large.csv")
